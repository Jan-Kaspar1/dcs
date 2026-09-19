"""Versioned QA run report contract and validator.

One report describes one Lenovo QA run against one exact main revision.
The schema is deliberately close to agent_pool.review's report contract:
stable keys, bounded fields, explicit outcomes, and validation that raises
ValueError on any violation. Reports are untrusted data until validated.

Schema version 3, top-level fields (v1/v2 documents remain valid input;
v2 added the optional verifications channel, v3 adds the optional mode
field, the exploration channel, and explicit per-scenario finding fields):

  schema_version           int, 1-3 (runners emit SCHEMA_VERSION)
  run_id                   stable run key: ^[a-z0-9][a-z0-9-]{0,79}$
  attempted_sha            40-hex main revision the run was launched against
  completed_sha            40-hex revision whose assessment completed, or
                           null when no assessment completed (blocked,
                           inconclusive, or interrupted runs). A passed or
                           failed outcome requires completed_sha ==
                           attempted_sha: an assessed revision may be
                           blocked by a capability gap, but a verdict
                           applies only to the revision actually tested.
  image                    {"controller": "sha256:...", "plant": "sha256:..."}
                           the digests of the exact artifacts tested, or
                           null when the run was blocked before any build
  started_at/finished_at   ISO-8601 timestamps, finished >= started
  outcome                  passed | failed | blocked | inconclusive |
                           interrupted
  attempt                  positive int retry counter (1 = first attempt)
  host                     {"name": str, "os": str} — sanitized host identity
  changed_range            {"first": sha, "last": sha} — the intervening
                           commit range this run's verdict covers when
                           queuing skipped intermediate revisions
  mode                     simulation | hardware-host | hardware-rig —
                           the rig the run exercised (schema v3; absent
                           means the legacy simulated-rig lane)
  scenarios                per-case results (see SCENARIO_FIELDS); schema
                           v3 allows the explicit finding fields in
                           SCENARIO_FIELDS_V3 so an exploratory session
                           can declare module, reproduction, severity,
                           confidence, and the worker regression contract
                           instead of the coordinator's generic defaults
  exploration              optional channel for charter-driven runs
                           (schema v3): charter, rationale, and the
                           recommended next frontiers the agent recorded
  verifications            fix-verification results (schema v2 only; see
                           VERIFICATION_FIELDS) — a verification run
                           replays one finding's original reproduction
                           case on a revision proven to contain its merged
                           fix and records the ancestry check, the verdict,
                           and the evidence here
  capability_limitations   [{"key","detail","blocking"}] known product gaps
                           that bounded this run (e.g. no EtherCAT driver)
  infrastructure_failures  [{"key","detail","phase"}] rig/build/credential/
                           agent failures, recorded separately from product
                           findings
  timeline                 [{"t","event","detail"}] action timeline
  notes                    optional free text

Consistency rules enforced beyond field shape:
  - outcome "passed" requires every scenario passed
  - outcome "failed" requires at least one failed scenario
  - completed_sha must equal attempted_sha for passed/failed outcomes
  - a passed/failed verification entry requires tested_sha ==
    completed_sha: a fix verdict applies only to the revision actually
    tested, and only to runs that completed an assessment of it
"""
import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 3
SUPPORTED_SCHEMAS = (1, 2, 3)
MAX_FIELD = 12000
MAX_SCENARIOS = 40
MAX_VERIFICATIONS = 40
MAX_TIMELINE = 200
MAX_OBSERVATIONS = 40
MAX_EVIDENCE = 20
MAX_LIMITATIONS = 40
MAX_INFRA_FAILURES = 40

KEY = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
GIT_SHA = re.compile(r'^[0-9a-f]{40}$')
IMAGE_DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')

RUN_OUTCOMES = ('passed', 'failed', 'blocked', 'inconclusive', 'interrupted')
CASE_OUTCOMES = ('passed', 'failed', 'blocked', 'inconclusive')
VERIFICATION_OUTCOMES = ('passed', 'failed', 'inconclusive')
EVIDENCE_KINDS = ('file', 'endpoint', 'log', 'metric')
RUN_MODES = ('simulation', 'hardware-host', 'hardware-rig')
SEVERITIES = ('low', 'medium', 'high', 'critical')
CONFIDENCES = ('low', 'medium', 'high')

TOP_LEVEL = {'schema_version', 'run_id', 'attempted_sha', 'completed_sha',
             'image', 'started_at', 'finished_at', 'outcome', 'attempt',
             'host', 'mode', 'changed_range', 'scenarios', 'verifications',
             'capability_limitations', 'infrastructure_failures', 'timeline',
             'notes', 'exploration'}
REQUIRED_TOP = TOP_LEVEL - {'attempt', 'host', 'mode', 'changed_range',
                            'verifications', 'notes', 'exploration'}

SCENARIO_FIELDS = {'key', 'title', 'expected', 'outcome', 'observations',
                   'evidence', 'detail'}
# Schema v3: an exploratory session declares the finding fields the
# coordinator would otherwise default — the affected module, the
# reproduction it actually ran, its own severity/confidence judgments,
# the worker regression contract, and whether the evidence identifies a
# product cause.
SCENARIO_FIELDS_V3 = {'module', 'mode', 'reproduction', 'severity',
                      'confidence', 'test_requirements', 'product_cause'}
REQUIRED_SCENARIO = SCENARIO_FIELDS - {'evidence', 'detail'}

VERIFICATION_FIELDS = {'finding_key', 'case', 'fix_sha', 'tested_sha',
                       'outcome', 'fix_ancestry', 'evidence', 'detail'}
REQUIRED_VERIFICATION = {'finding_key', 'case', 'fix_sha', 'tested_sha',
                         'outcome'}
ANCESTRY_FIELDS = {'checked', 'contained', 'method', 'detail'}

EXPLORATION_FIELDS = {'charter', 'rationale', 'next_frontiers', 'agent'}


def _bounded_text(value, field, limit=MAX_FIELD):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Missing or oversized text: ' + field)
    return value


def _bounded_list(value, field, item_limit=MAX_FIELD, count=40):
    if not isinstance(value, list) or len(value) > count:
        raise ValueError('Invalid list: ' + field)
    for entry in value:
        _bounded_text(entry, field, item_limit)
    return value


def _iso(value, field):
    if not isinstance(value, str):
        raise ValueError(field + ' must be an ISO-8601 string')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(field + ' must be an ISO-8601 string')


def _git_sha(value, field):
    if not isinstance(value, str) or not GIT_SHA.match(value):
        raise ValueError(field + ' must be a 40-hex git revision')
    return value


def _keyed_detail(item, field, extra_required=()):
    """Shared shape for capability limitations and infrastructure failures."""
    if not isinstance(item, dict):
        raise ValueError(field + ' entries must be objects')
    required = {'key', 'detail'} | set(extra_required)
    if not required <= set(item):
        raise ValueError(field + ' entry missing fields: ' + ','.join(sorted(required - set(item))))
    if not isinstance(item['key'], str) or not KEY.match(item['key']):
        raise ValueError('Invalid ' + field + ' key')
    _bounded_text(item['detail'], field + '.detail', 4000)
    return item


def _validate_evidence(item, scenario_key):
    evidence = item['evidence']
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise ValueError('Invalid evidence list in scenario ' + scenario_key)
    for entry in evidence:
        if not isinstance(entry, dict) or not {'kind', 'ref'} <= set(entry) \
                or not set(entry) <= {'kind', 'ref', 'detail'}:
            raise ValueError('Invalid evidence entry in scenario ' + scenario_key)
        if entry['kind'] not in EVIDENCE_KINDS:
            raise ValueError('Invalid evidence kind in scenario ' + scenario_key)
        ref = entry['ref']
        if not isinstance(ref, str) or not ref or len(ref) > 500:
            raise ValueError('Invalid evidence ref in scenario ' + scenario_key)
        if entry['kind'] == 'file':
            # File evidence must name a path inside the run's results
            # directory — never an absolute or escaping host path.
            if ref.startswith('/') or re.match(r'^[A-Za-z]:', ref) \
                    or '..' in Path(ref).parts:
                raise ValueError('Evidence file ref must be relative: ' + scenario_key)
        if 'detail' in entry:
            _bounded_text(entry['detail'], 'evidence.detail', 2000)


def validate_scenario(item, version=SCHEMA_VERSION):
    allowed = set(SCENARIO_FIELDS)
    if version >= 3:
        allowed |= SCENARIO_FIELDS_V3
    if not isinstance(item, dict) or not set(item) <= allowed:
        raise ValueError('Invalid scenario fields')
    missing = REQUIRED_SCENARIO - set(item)
    if missing:
        raise ValueError('Scenario missing fields: ' + ','.join(sorted(missing)))
    if not isinstance(item['key'], str) or not KEY.match(item['key']):
        raise ValueError('Invalid scenario key')
    _bounded_text(item['title'], 'scenario.title', 200)
    _bounded_text(item['expected'], 'scenario.expected', 4000)
    if item['outcome'] not in CASE_OUTCOMES:
        raise ValueError('Invalid scenario outcome')
    _bounded_list(item['observations'], 'scenario.observations', 2000,
                  MAX_OBSERVATIONS)
    if 'evidence' in item:
        _validate_evidence(item, item['key'])
    if 'detail' in item:
        _bounded_text(item['detail'], 'scenario.detail', 4000)
    if 'module' in item:
        _bounded_text(item['module'], 'scenario.module', 500)
    if 'mode' in item and item['mode'] not in RUN_MODES:
        raise ValueError('Invalid scenario mode')
    if 'reproduction' in item:
        _bounded_text(item['reproduction'], 'scenario.reproduction', 8000)
    if 'severity' in item and item['severity'] not in SEVERITIES:
        raise ValueError('Invalid scenario severity')
    if 'confidence' in item and item['confidence'] not in CONFIDENCES:
        raise ValueError('Invalid scenario confidence')
    if 'test_requirements' in item:
        _bounded_text(item['test_requirements'],
                      'scenario.test_requirements', 4000)
    if 'product_cause' in item and type(item['product_cause']) is not bool:
        raise ValueError('scenario.product_cause must be boolean')
    return item


def validate_exploration(item):
    """The exploration channel a charter-driven run records: which
    charter it pursued, why it was novel, and which frontiers it
    recommends next."""
    if not isinstance(item, dict) or not set(item) <= EXPLORATION_FIELDS:
        raise ValueError('Invalid exploration fields')
    if 'charter' in item:
        _bounded_text(item['charter'], 'exploration.charter', 500)
    if 'rationale' in item:
        _bounded_text(item['rationale'], 'exploration.rationale', 4000)
    if 'next_frontiers' in item:
        _bounded_list(item['next_frontiers'], 'exploration.next_frontiers',
                      500, 10)
    if 'agent' in item:
        _bounded_text(item['agent'], 'exploration.agent', 200)
    return item


def validate_verification(item):
    """One fix-verification result recorded by a verification run.

    finding_key names the finding the run certifies; case is the original
    reproduction's case identity (the scenario key that produced the
    finding). fix_sha is the merged fix commit; tested_sha the revision
    the case actually ran against. fix_ancestry records the runner's
    containment check (git merge-base --is-ancestor) so a verdict is
    never claimed on an untested or non-containing revision.
    """
    if not isinstance(item, dict) or not set(item) <= VERIFICATION_FIELDS:
        raise ValueError('Invalid verification fields')
    missing = REQUIRED_VERIFICATION - set(item)
    if missing:
        raise ValueError('Verification missing fields: '
                         + ','.join(sorted(missing)))
    for field in ('finding_key', 'case'):
        if not isinstance(item[field], str) or not KEY.match(item[field]):
            raise ValueError('Invalid verification ' + field)
    _git_sha(item['fix_sha'], 'verification.fix_sha')
    _git_sha(item['tested_sha'], 'verification.tested_sha')
    if item['outcome'] not in VERIFICATION_OUTCOMES:
        raise ValueError('Invalid verification outcome')
    ancestry = item.get('fix_ancestry')
    if ancestry is not None:
        if not isinstance(ancestry, dict) \
                or not set(ancestry) <= ANCESTRY_FIELDS:
            raise ValueError('Invalid verification fix_ancestry')
        if 'checked' in ancestry and type(ancestry['checked']) is not bool:
            raise ValueError('fix_ancestry.checked must be boolean')
        if 'contained' in ancestry \
                and ancestry['contained'] is not None and type(ancestry['contained']) is not bool:
            raise ValueError('fix_ancestry.contained must be boolean')
        if ancestry.get('method') is not None:
            _bounded_text(ancestry['method'], 'fix_ancestry.method', 200)
        if ancestry.get('detail') is not None:
            _bounded_text(ancestry['detail'], 'fix_ancestry.detail', 2000)
    evidence = item.get('evidence')
    if evidence is not None:
        if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
            raise ValueError('Invalid verification evidence')
        for entry in evidence:
            if isinstance(entry, str):
                _bounded_text(entry, 'verification.evidence', 2000)
                continue
            if not isinstance(entry, dict) \
                    or not {'detail'} <= set(entry) <= {'detail', 'source'}:
                raise ValueError('Invalid verification evidence entry')
            _bounded_text(entry['detail'], 'verification.evidence', 2000)
            if entry.get('source') is not None:
                _bounded_text(entry['source'], 'evidence.source', 500)
    if 'detail' in item:
        _bounded_text(item['detail'], 'verification.detail', 4000)
    return item


def validate_report(text, run_id=None, attempted_sha=None):
    """Parse and validate a run report; raise ValueError on any violation.

    `run_id` and `attempted_sha` optionally pin the report to the run the
    supervisor launched — the same convention review.validate_report uses
    to bind a report to its invocation.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError('Report is not JSON: ' + str(exc))
    if not isinstance(data, dict) or not REQUIRED_TOP <= set(data) <= TOP_LEVEL:
        raise ValueError('Invalid report top-level fields')
    if data['schema_version'] not in SUPPORTED_SCHEMAS:
        raise ValueError('Unsupported report schema version')
    if data['schema_version'] < 2 and 'verifications' in data:
        raise ValueError('verifications require report schema version 2')
    if data['schema_version'] < 3 \
            and ('exploration' in data or 'mode' in data):
        raise ValueError('mode and exploration require report schema version 3')
    if 'mode' in data and data['mode'] not in RUN_MODES:
        raise ValueError('Invalid run mode')
    if 'exploration' in data:
        validate_exploration(data['exploration'])
    if not isinstance(data['run_id'], str) or not KEY.match(data['run_id']):
        raise ValueError('Invalid run_id')
    if run_id is not None and data['run_id'] != run_id:
        raise ValueError('Report run_id does not match the launched run')
    _git_sha(data['attempted_sha'], 'attempted_sha')
    if attempted_sha is not None and data['attempted_sha'] != attempted_sha:
        raise ValueError('Report attempted_sha does not match the launched run')
    completed = data['completed_sha']
    if completed is not None:
        _git_sha(completed, 'completed_sha')
    started, finished = _iso(data['started_at'], 'started_at'), \
        _iso(data['finished_at'], 'finished_at')
    if finished < started:
        raise ValueError('finished_at precedes started_at')
    if data['outcome'] not in RUN_OUTCOMES:
        raise ValueError('Invalid run outcome')
    if 'attempt' in data and (type(data['attempt']) is not int
                              or data['attempt'] < 1):
        raise ValueError('attempt must be a positive integer')

    image = data['image']
    if image is not None:
        if not isinstance(image, dict) \
                or not {'controller', 'plant'} <= set(image) \
                or not set(image) <= {'controller', 'plant'}:
            raise ValueError('image must name controller and plant digests')
        for name, digest in image.items():
            if not isinstance(digest, str) or not IMAGE_DIGEST.match(digest):
                raise ValueError('image.' + name
                                 + ' must be a sha256 digest')

    if 'host' in data:
        host = data['host']
        if not isinstance(host, dict) or not {'name', 'os'} <= set(host) \
                or not set(host) <= {'name', 'os'}:
            raise ValueError('host must contain name and os')
        _bounded_text(host['name'], 'host.name', 200)
        _bounded_text(host['os'], 'host.os', 500)

    if 'changed_range' in data:
        rng = data['changed_range']
        if not isinstance(rng, dict) or set(rng) != {'first', 'last'}:
            raise ValueError('changed_range must contain first and last')
        _git_sha(rng['first'], 'changed_range.first')
        _git_sha(rng['last'], 'changed_range.last')
        if rng['last'] != data['attempted_sha']:
            raise ValueError('changed_range.last must equal attempted_sha')

    scenarios = data['scenarios']
    if not isinstance(scenarios, list) or len(scenarios) > MAX_SCENARIOS:
        raise ValueError('Invalid scenarios list')
    seen = set()
    case_outcomes = []
    for item in scenarios:
        validate_scenario(item, data['schema_version'])
        if item['key'] in seen:
            raise ValueError('Duplicate scenario key')
        seen.add(item['key'])
        case_outcomes.append(item['outcome'])

    outcome = data['outcome']
    if outcome == 'passed' and (not case_outcomes
                                or any(c != 'passed' for c in case_outcomes)):
        raise ValueError('passed outcome requires every scenario passed')
    if outcome == 'failed' and 'failed' not in case_outcomes:
        raise ValueError('failed outcome requires a failed scenario')
    if outcome in ('passed', 'failed') and completed != data['attempted_sha']:
        raise ValueError('passed/failed requires completed_sha == attempted_sha')

    verifications = data.get('verifications') or []
    if not isinstance(verifications, list) \
            or len(verifications) > MAX_VERIFICATIONS:
        raise ValueError('Invalid verifications list')
    seen = set()
    for item in verifications:
        validate_verification(item)
        if item['finding_key'] in seen:
            raise ValueError('Duplicate verification finding_key')
        seen.add(item['finding_key'])
        if item['outcome'] in ('passed', 'failed') \
                and item['tested_sha'] != completed:
            raise ValueError('passed/failed verification requires '
                           'tested_sha == completed_sha')

    limitations = data['capability_limitations']
    if not isinstance(limitations, list) or len(limitations) > MAX_LIMITATIONS:
        raise ValueError('Invalid capability_limitations list')
    for item in limitations:
        _keyed_detail(item, 'capability_limitations')
        if 'blocking' in item and type(item['blocking']) is not bool:
            raise ValueError('capability_limitations.blocking must be boolean')

    failures = data['infrastructure_failures']
    if not isinstance(failures, list) or len(failures) > MAX_INFRA_FAILURES:
        raise ValueError('Invalid infrastructure_failures list')
    for item in failures:
        _keyed_detail(item, 'infrastructure_failures')
        if 'phase' in item:
            _bounded_text(item['phase'], 'infrastructure_failures.phase', 200)

    timeline = data['timeline']
    if not isinstance(timeline, list) or len(timeline) > MAX_TIMELINE:
        raise ValueError('Invalid timeline')
    for entry in timeline:
        if not isinstance(entry, dict) or not {'t', 'event'} <= set(entry) \
                or not set(entry) <= {'t', 'event', 'detail'}:
            raise ValueError('Invalid timeline entry')
        _iso(entry['t'], 'timeline.t')
        _bounded_text(entry['event'], 'timeline.event', 200)
        if 'detail' in entry:
            _bounded_text(entry['detail'], 'timeline.detail', 2000)

    if 'notes' in data:
        _bounded_text(data['notes'], 'notes', 4000)
    return data
