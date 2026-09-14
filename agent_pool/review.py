"""Daily autonomous architecture review lane: schedule, verify, validate.

The supervisor drives the lane; this module holds the pure mechanics:
schedule computation, resource-hash preflight, report validation, prompt
assembly, coverage cursor, and report retention. Reports live outside Git.
"""
import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib on supported hosts
    ZoneInfo = None

SCHEMA_VERSION = 1
RESOURCE_DIR = Path('agent_pool/resources/architecture')
# Repository areas the coverage cursor rotates through so periodic reviews
# inspect quieter code as well as hotspots.
AREAS = ('crates/dcs-core', 'crates/dcs-model', 'crates/dcs-runtime',
         'crates/dcs-sim', 'crates/dcs-blocks', 'crates/dcs-monitor',
         'crates/dcs-demo', 'agent_pool', 'scripts', 'docs')
DEFAULTS = {'enabled': False, 'mode': 'report', 'auto_promote': True,
            'time': '03:00', 'timezone': 'Europe/Berlin', 'timeout_seconds': 3600,
            'max_candidates': 3, 'retention_days': 30}
MAX_FIELD = 12000
KEY = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
SHA = re.compile(r'^[0-9a-f]{64}$')
TOP_LEVEL = {'schema_version', 'run_id', 'base_sha', 'previous_reviewed_sha',
             'started_at', 'ended_at', 'policy_sha256', 'prompt_sha256',
             'references_sha256', 'status', 'scope', 'missing_context',
             'candidates', 'assessments', 'notes'}
REQUIRED_TOP = TOP_LEVEL - {'notes'}
CANDIDATE_FIELDS = {'key', 'title', 'outcome', 'evidence', 'affected_modules',
                    'problem', 'proposal', 'caller_benefit', 'invariants',
                    'test_approach', 'alternatives', 'confidence', 'effort',
                    'risk', 'related_issues', 'decision_conflicts', 'naming'}


def settings(config):
    """Validate and default the review section of the pool configuration."""
    cfg = dict(DEFAULTS)
    cfg.update(config.get('review') or {})
    if type(cfg['enabled']) is not bool or type(cfg['auto_promote']) is not bool:
        raise ValueError('review.enabled and review.auto_promote must be boolean')
    if cfg['mode'] not in ('report', 'pilot', 'full'):
        raise ValueError('review.mode must be report, pilot, or full')
    try:
        hour, minute = (int(part) for part in str(cfg['time']).split(':'))
    except ValueError:
        raise ValueError('review.time must be HH:MM')
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('review.time must be HH:MM')
    cfg['hour'], cfg['minute'] = hour, minute
    for key in ('timeout_seconds', 'max_candidates', 'retention_days'):
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ValueError('review.' + key + ' must be a positive integer')
    return cfg


def current_slot(now, cfg):
    """Epoch of the most recent scheduled instant at or before `now`.

    One slot per local day keeps missed runs single-shot: a supervisor that
    resumes after the scheduled time owes exactly one run for that slot.
    """
    tz = ZoneInfo(cfg['timezone']) if ZoneInfo else timezone.utc
    local = datetime.fromtimestamp(now, tz)
    scheduled = local.replace(hour=cfg['hour'], minute=cfg['minute'],
                              second=0, microsecond=0)
    if scheduled.timestamp() > now:
        scheduled = (local - timedelta(days=1)).replace(
            hour=cfg['hour'], minute=cfg['minute'], second=0, microsecond=0)
    return int(scheduled.timestamp())


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(base):
    """File->sha256 map for every resource file, excluding MANIFEST.json."""
    base = Path(base)
    files = {}
    for path in sorted(base.rglob('*')):
        if path.is_file() and path.name != 'MANIFEST.json':
            files[path.relative_to(base).as_posix()] = sha256(path)
    return files


def write_manifest(base, upstream_revision):
    base = Path(base)
    document = {
        'schema': 1,
        'upstream': {
            'repository': 'https://github.com/mattpocock/skills',
            'license': 'MIT',
            'revision': upstream_revision,
            'provenance': 'local copies under C:/Users/Kaspar/.codex/skills inspected 2026-09-14',
        },
        'project_owned': ['policy.md', 'prompt.md', 'NOTICE.md'],
        'files': manifest(base),
        'adaptations': [],
    }
    (base / 'MANIFEST.json').write_text(json.dumps(document, indent=2) + '\n')
    return document


def installed_root():
    """The pinned installation root whose resources govern reviewer behavior.

    Policy, prompt, and references deploy with the supervisor release, so the
    reviewer must read the installed copies — not whatever the source revision
    under review happens to carry.
    """
    return Path(__file__).resolve().parents[1]


def stage_resources(dest):
    """Verify the installed resources and copy them beside the report.

    Returns the recorded resource hashes. Raises ValueError on any manifest
    mismatch — a failed preflight must abort the run explicitly, never launch
    with untrusted reference material.
    """
    hashes = verify_resources(installed_root())
    try:
        shutil.copytree(Path(installed_root()) / RESOURCE_DIR, Path(dest),
                        dirs_exist_ok=True)
    except Exception as exc:
        raise RuntimeError('Could not stage review resources: ' + str(exc))
    return hashes


def verify_resources(clone):
    """Check the checkout's resources against their manifest; return hashes.

    Raises ValueError listing every missing or mismatched file — a failed
    preflight must abort the run explicitly, never launch with untrusted
    reference material.
    """
    base = Path(clone) / RESOURCE_DIR
    manifest_path = base / 'MANIFEST.json'
    if not manifest_path.is_file():
        raise ValueError('Missing architecture resource manifest')
    recorded = json.loads(manifest_path.read_text()).get('files', {})
    actual = manifest(base)
    bad = [name for name in set(recorded) | set(actual)
           if recorded.get(name) != actual.get(name)]
    if bad:
        raise ValueError('Architecture resource hash mismatch: ' + ', '.join(sorted(bad)))
    return {'policy_sha256': actual['policy.md'],
            'prompt_sha256': actual['prompt.md'],
            'references_sha256': sha256(manifest_path)}


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


def validate_candidate(item, clone, known_issues, field='candidates'):
    if not isinstance(item, dict) or not set(item) <= CANDIDATE_FIELDS:
        raise ValueError('Invalid candidate fields in ' + field)
    missing = CANDIDATE_FIELDS - {'naming'} - set(item)
    if missing:
        raise ValueError('Candidate missing fields: ' + ','.join(sorted(missing)))
    if not isinstance(item['key'], str) or not KEY.match(item['key']):
        raise ValueError('Invalid candidate key')
    _bounded_text(item['title'], 'title', 200)
    if item['outcome'] not in ('depth', 'naming', 'both'):
        raise ValueError('Candidate outcome must be depth, naming, or both')
    if not isinstance(item['evidence'], list) or not item['evidence'] or len(item['evidence']) > 20:
        raise ValueError('Candidate requires 1-20 evidence entries')
    for entry in item['evidence']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'detail'}:
            raise ValueError('Evidence entries need path and detail')
        path = entry['path']
        if (not isinstance(path, str) or not path or path.startswith('/')
                or re.match(r'^[A-Za-z]:', path) or '..' in Path(path).parts):
            raise ValueError('Evidence path must be a relative repository path')
        if clone is not None and not (Path(clone) / path).exists():
            raise ValueError('Evidence path absent at base revision: ' + path)
        _bounded_text(entry['detail'], 'evidence.detail', 2000)
    _bounded_list(item['affected_modules'], 'affected_modules', 500)
    for name in ('problem', 'proposal', 'caller_benefit', 'test_approach', 'alternatives'):
        _bounded_text(item[name], name)
    _bounded_list(item['invariants'], 'invariants', 2000)
    for name in ('confidence', 'effort', 'risk'):
        allowed = {'confidence': ('low', 'medium', 'high'),
                   'effort': ('small', 'medium', 'large'),
                   'risk': ('low', 'medium', 'high')}[name]
        if item[name] not in allowed:
            raise ValueError('Invalid ' + name)
    if not isinstance(item['related_issues'], list) or len(item['related_issues']) > 20:
        raise ValueError('Invalid related_issues')
    for number in item['related_issues']:
        if type(number) is not int or number < 1:
            raise ValueError('related_issues must be positive integers')
        if known_issues is not None and number not in known_issues:
            raise ValueError('Candidate references unknown issue ' + str(number))
    _bounded_list(item['decision_conflicts'], 'decision_conflicts', 2000)
    naming = item.get('naming')
    if item['outcome'] in ('naming', 'both'):
        if not isinstance(naming, dict) or not set(naming) <= {'canonical', 'aliases', 'compatibility'}:
            raise ValueError('Naming candidates require a naming object')
        _bounded_text(naming.get('canonical'), 'naming.canonical', 200)
        _bounded_list(naming.get('aliases'), 'naming.aliases', 500)
        _bounded_text(naming.get('compatibility'), 'naming.compatibility', 4000)
    elif naming is not None and not isinstance(naming, dict):
        raise ValueError('naming must be an object or null')
    return item


def validate_report(text, clone, run_id, base_sha, known_issues=None,
                    max_candidates=3, expected_hashes=None):
    """Parse and validate a report; raise ValueError on any violation."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError('Report is not JSON: ' + str(exc))
    if not isinstance(data, dict) or not REQUIRED_TOP <= set(data) <= TOP_LEVEL:
        raise ValueError('Invalid report top-level fields')
    if data['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported report schema version')
    if data['run_id'] != run_id:
        raise ValueError('Report run_id does not match the launched run')
    if data['base_sha'] != base_sha:
        raise ValueError('Report base_sha does not match the pinned checkout')
    if data['previous_reviewed_sha'] is not None and not isinstance(data['previous_reviewed_sha'], str):
        raise ValueError('previous_reviewed_sha must be a string or null')
    started, ended = _iso(data['started_at'], 'started_at'), _iso(data['ended_at'], 'ended_at')
    if ended < started:
        raise ValueError('ended_at precedes started_at')
    for field in ('policy_sha256', 'prompt_sha256', 'references_sha256'):
        if not isinstance(data[field], str) or not SHA.match(data[field]):
            raise ValueError(field + ' must be a sha256 hex digest')
        if expected_hashes and data[field] != expected_hashes[field]:
            raise ValueError(field + ' does not match the verified resources')
    if data['status'] not in ('completed', 'inconclusive', 'skipped'):
        raise ValueError('Invalid report status')
    _bounded_list(data['scope'], 'scope', 500)
    _bounded_list(data['missing_context'], 'missing_context', 2000)
    if 'notes' in data:
        _bounded_text(data['notes'], 'notes', 4000)
    candidates = data['candidates']
    if not isinstance(candidates, list) or len(candidates) > max_candidates:
        raise ValueError('Report exceeds the candidate limit')
    seen = set()
    for item in candidates:
        validate_candidate(item, clone, known_issues)
        if item['key'] in seen:
            raise ValueError('Duplicate candidate key')
        seen.add(item['key'])
    if not isinstance(data['assessments'], list) or len(data['assessments']) > 20:
        raise ValueError('Invalid assessments list')
    for assessment in data['assessments']:
        if not isinstance(assessment, dict) or not {'key', 'outcome', 'observed'} <= set(assessment) <= {'key', 'outcome', 'observed', 'followup_candidate'}:
            raise ValueError('Invalid assessment fields')
        if not isinstance(assessment['key'], str) or not KEY.match(assessment['key']):
            raise ValueError('Invalid assessment key')
        if assessment['outcome'] not in ('confirmed', 'rejected', 'followup'):
            raise ValueError('Invalid assessment outcome')
        _bounded_text(assessment['observed'], 'assessment.observed', 4000)
        followup = assessment.get('followup_candidate')
        if assessment['outcome'] == 'followup' and followup is not None:
            validate_candidate(followup, clone, known_issues, 'followup_candidate')
            if followup['key'] in seen:
                raise ValueError('Follow-up candidate duplicates another key')
            seen.add(followup['key'])
        elif followup is not None:
            raise ValueError('followup_candidate requires the followup outcome')
    return data


def prompt(template, run_id, base_sha, previous_sha, report_dir, resource_dir,
           context, cfg):
    text = template.replace('__RUN_ID__', run_id)
    text = text.replace('__BASE_SHA__', base_sha)
    text = text.replace('__PREVIOUS_SHA__', previous_sha or 'null')
    text = text.replace('__REPORT_DIR__', str(report_dir))
    text = text.replace('__RESOURCE_DIR__', str(resource_dir))
    text = text.replace('__MAX_CANDIDATES__', str(cfg['max_candidates']))
    text = text.replace('__COVERAGE_FOCUS__', context.get('coverage_focus', 'unspecified'))
    return text.replace('__RUN_CONTEXT__', json.dumps(context, indent=1))


def prune_reports(root, now, retention_days, protected=()):
    """Remove finished report directories older than the retention bound."""
    removed = []
    base = Path(root) / 'reviews'
    cutoff = now - retention_days * 86400
    for path in sorted(base.glob('*')) if base.is_dir() else []:
        if not path.is_dir() or path.name in protected or path.stat().st_mtime > cutoff:
            continue
        shutil.rmtree(path)
        removed.append(path.name)
    return removed
