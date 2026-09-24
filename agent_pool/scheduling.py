"""Pure work eligibility and planning-demand decisions for the coordinator."""
from collections import Counter
import time
from . import areas, planning

def classify(issue, jobs, closed, active_improvement=None):
    """Return (reason, metadata) without changing GitHub or scheduler state."""
    number = issue['number']
    if issue.get('state') != 'OPEN':
        return 'closed', None
    if number in jobs:
        return jobs[number]['status'], None
    labels = {row['name'] for row in issue.get('labels', ())}
    if 'agent:ready' not in labels:
        return 'not-ready', None
    try:
        meta = planning.metadata(issue.get('body') or '')
    except (ValueError, KeyError):
        return 'invalid-metadata', None
    if not (meta.get('area') or areas.issue_area(issue)):
        return 'missing-area', meta
    if set(meta['dependencies']) - closed:
        return 'dependency', meta
    improvement = meta.get('improvement')
    if improvement and active_improvement and improvement != active_improvement:
        return 'improvement-lane', meta
    return 'ready', meta

def inventory(issues, jobs, active_improvement=None):
    """One read model for dispatch eligibility and operator explanation."""
    by_number = {job['issue']: job for job in jobs}
    closed = {issue['number'] for issue in issues if issue.get('state') == 'CLOSED'}
    rows = []
    for issue in issues:
        reason, meta = classify(issue, by_number, closed, active_improvement)
        if reason == 'closed':
            continue
        rows.append({
            'issue': issue['number'],
            'reason': reason,
            'priority': meta.get('priority') if meta else None,
            'area': (meta.get('area') or areas.issue_area(issue)) if meta else areas.issue_area(issue),
            'missing_dependencies': sorted(set(meta['dependencies']) - closed)
                if reason == 'dependency' else [],
        })
    return rows

def due_for_planning(now, last_plan, ready_count, armed_retries, *, forced=False):
    """Run periodic planning, or earlier when the work frontier is thin."""
    elapsed = now - last_plan
    return forced or elapsed >= 7200 or (elapsed >= 900 and ready_count + armed_retries < 6)

def explain(issues, jobs, admission, retry_issues=(), active_improvement=None, now=None, configured_groups=None):
    """Read-only, compact explanation of factory demand and provider limits."""
    now = time.time() if now is None else now
    rows = inventory(issues, jobs, active_improvement)
    counts = Counter(row['reason'] for row in rows)
    groups = {}
    for name, group in admission['groups'].items():
        if configured_groups is not None and name not in configured_groups:
            continue
        limit = group['target']
        reason = None
        if group['mode'] == 'blocked':
            reason = 'provider-blocked'
        elif group['cooldown_until'] and now < group['cooldown_until']:
            reason = 'cooldown'
        elif group['mode'] == 'probing':
            reason = 'single-probe'
        elif group['active'] >= limit - group['external_slots']:
            reason = 'target-full'
        groups[name] = {
            'target': limit,
            'active': group['active'],
            'mode': group['mode'],
            'limiting_reason': reason,
            'cooldown_until': group['cooldown_until'],
        }
    return {
        'work_counts': dict(sorted(counts.items())),
        'armed_retries': sorted(retry_issues),
        'provider_groups': groups,
        'issues': sorted(rows, key=lambda row: row['issue']),
    }
