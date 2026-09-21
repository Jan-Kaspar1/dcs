"""Product-area taxonomy and investment allocation for the DCS backlog.

An area describes the product capability that benefits from the work, not the
crate, validation venue, or worker that happens to implement it. Every managed
open issue owns exactly one ``area:*`` label and matching metadata value.
"""
from collections import Counter
from dataclasses import dataclass, field
import json

PREFIX = '<!-- dcs-task:'

DEFINITIONS = {
    'engineering': (15, 'Engineering model, SDK, composition, and authoring experience'),
    'control-runtime': (15, 'Deterministic execution, commands, state, and scan semantics'),
    'high-availability': (15, 'Redundancy, failover, convergence, and ownership integrity'),
    'field-connectivity': (10, 'Drivers, fieldbuses, cyclic exchange, and physical I/O'),
    'operations': (10, 'Operator UI, monitoring, history, and runtime observability'),
    'alarms-diagnostics': (10, 'Alarm lifecycle, diagnostics, attribution, and event evidence'),
    'library': (10, 'Reusable control blocks, equipment modules, and process patterns'),
    'deployment-lifecycle': (5, 'Packaging, release, upgrade, compatibility, and rollout'),
    'verification': (5, 'Test infrastructure, conformance, simulation, and QA machinery'),
    'delivery-platform': (5, 'Agent factory, CI, repository automation, and contributor flow'),
}
NAMES = tuple(DEFINITIONS)


def validate(area):
    if area not in DEFINITIONS:
        raise ValueError('Invalid product area: ' + str(area))
    return area


def label(area):
    return 'area:' + validate(area)


def issue_area(issue):
    """Return the single valid area recorded by labels or metadata."""
    labels = [row['name'][5:] for row in issue.get('labels', [])
              if row.get('name', '').startswith('area:')]
    valid = [value for value in labels if value in DEFINITIONS]
    if len(valid) == 1:
        return valid[0]
    body = issue.get('body') or ''
    if body.startswith(PREFIX):
        try:
            value = json.loads(body[len(PREFIX):body.index(' -->')]).get('area')
            return value if value in DEFINITIONS else None
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return None


def infer(module, *text):
    """Classify a QA finding by benefited capability, then technical module."""
    words = ' '.join((module,) + text).lower()
    keyword_areas = (
        ('high-availability', ('standby', 'failover', 'promot', 'demot', 'checkpoint',
                               'receipt', 'reconverg', 'tracking', 'fencing', 'ownership')),
        ('alarms-diagnostics', ('alarm', 'acknowledg', 'latch', 'shelv', 'suppress',
                                'first-out', 'diagnostic')),
        ('field-connectivity', ('ethercat', 'fieldbus', 'cyclic', 'driver', 'fanout',
                                'physical i/o', 'wago')),
        ('operations', ('monitor', 'operator', 'history page', 'cursor', 'fetch', 'dashboard')),
        ('library', ('pump', 'filter', 'aeration', 'dosing', 'equipment module')),
        ('deployment-lifecycle', ('release', 'upgrade', 'migration', 'compatib')),
    )
    for area, needles in keyword_areas:
        if any(needle in words for needle in needles):
            return area
    tail = module.strip().rstrip('/').split('/')[-1].lower()
    return {
        'agent_pool': 'delivery-platform', 'ci': 'delivery-platform',
        'qa_lane': 'verification', 'qa-hardware': 'verification',
        'dcs-build': 'engineering', 'model': 'engineering',
        'dcs-controller': 'control-runtime', 'dcs-runtime': 'control-runtime',
        'dcs-monitor': 'operations', 'dcs-blocks': 'library',
        'reference-plant': 'library', 'dcs-assembly': 'field-connectivity',
        'dcs-ethercat': 'field-connectivity', 'dcs-sim-net': 'field-connectivity',
    }.get(tail, 'control-runtime')


@dataclass
class Allocation:
    """Rolling investment view used only after priority has been respected."""
    recent: Counter = field(default_factory=Counter)
    active: Counter = field(default_factory=Counter)
    open: Counter = field(default_factory=Counter)
    ready: Counter = field(default_factory=Counter)

    @classmethod
    def from_inventory(cls, issues, jobs, window=30):
        by_number = {issue['number']: issue for issue in issues}
        result = cls()
        for issue in issues:
            area = issue_area(issue)
            if not area or issue.get('state') != 'OPEN':
                continue
            result.open[area] += 1
            if 'agent:ready' in [row['name'] for row in issue.get('labels', [])]:
                result.ready[area] += 1
        completed = sorted((job for job in jobs if job.get('status') == 'done'),
                           key=lambda job: job.get('updated') or '', reverse=True)[:window]
        for job in completed:
            area = issue_area(by_number.get(job['issue'], {}))
            if area:
                result.recent[area] += 1
        for job in jobs:
            if job.get('status') not in ('working', 'pr-open'):
                continue
            area = issue_area(by_number.get(job['issue'], {}))
            if area:
                result.active[area] += 1
        return result

    def score(self, area):
        validate(area)
        weight = DEFINITIONS[area][0]
        return (self.recent[area] + self.active[area]) / weight

    def note(self, area):
        self.active[validate(area)] += 1

    def summary(self):
        return {
            area: {'target_percent': weight, 'recent_completed': self.recent[area],
                   'active': self.active[area], 'open': self.open[area],
                   'ready': self.ready[area]}
            for area, (weight, _description) in DEFINITIONS.items()
        }
