"""One completion-oriented work queue over the durable job and lease ledgers.

The existing supervisor remains the sole process and GitHub writer. This module
owns demand selection; quota is a supply condition, never a lifetime job budget.
Checkouts waiting for delivery occupy workspaces, not inference worker slots.
"""
from pathlib import Path
from . import areas, planning, scheduling


class Factory:
    def __init__(self, supervisor):
        self.s = supervisor
        self.config = supervisor.config['factory']

    def defer_repair(self, job, reason, cause, detail):
        s = self.s
        key = 'repair:' + str(job['issue'])
        if not s.state.get(key):
            s.state.set(key, {'reason': reason, 'cause': cause, 'detail': detail})
        s.admission.release('job:' + str(job['issue']))

    def defer_timeout(self, job):
        """Queue a bounded continuation only after work was safely captured."""
        s = self.s
        n = job['issue']
        rec = s.state.get('recovery:' + str(n)) or {}
        used = rec.get('timeout_requeues', 0)
        limit = self.config.get('max_timeout_retries', 2)
        if (rec.get('work') is not True or rec.get('phase') != 'captured'
                or job['repairs'] >= 3 or used >= limit):
            return
        delay = self.config.get('timeout_retry_delay_seconds', 60)
        rec.update(timeout_requeues=used + 1,
                   requeue={'source': 'timeout', 'seconds': delay,
                            'not_before': s.clock() + delay})
        s.state.set('recovery:' + str(n), rec)
        s.state.set('retry:' + str(n), 'timeout-requeue')
        s.log(f'#{n} timeout continuation queued ({used + 1}/{limit}); '
              f'preserved work, retry after {delay}s')

    def demand(self, issues):
        """Return all valid worker demand, including delayed provider waits."""
        s = self.s
        jobs = {j['issue']: j for j in s.state.jobs()}
        closed = {i['number'] for i in issues if i.get('state') == 'CLOSED'}
        allocation = areas.Allocation.from_inventory(issues, list(jobs.values()))
        result = []
        for issue in issues:
            n = issue['number']
            if issue.get('state') != 'OPEN':
                continue
            try:
                meta = planning.metadata(issue.get('body') or '')
            except (ValueError, KeyError):
                continue
            if set(meta['dependencies']) - closed:
                continue
            job = jobs.get(n)
            repair = s.state.get('repair:' + str(n))
            rec = s.state.get('recovery:' + str(n)) or {}
            flag = s.state.get('retry:' + str(n))
            if job and job['status'] in ('working', 'pr-open') and repair:
                kind, not_before, category = 'repair', 0, 0
            elif job and job['status'] == 'blocked' and (flag or rec.get('provider_wait')):
                if rec.get('phase') == 'error' and flag is not True:
                    continue
                kind, category = 'retry', 1 if rec.get('work') else 2
                not_before = (rec.get('requeue') or {}).get('not_before', 0) if flag is not True else 0
            elif job is None:
                reason, _ = scheduling.classify(issue, jobs, closed,
                                                s.state.get('review:active_improvement'))
                if reason != 'ready':
                    continue
                kind, not_before, category = 'new', 0, 2
            else:
                continue
            area = meta.get('area') or areas.issue_area(issue)
            rank = (meta.get('priority', 3), category,
                    allocation.score(area) if area else float('inf'),
                    (job or {}).get('updated', 0), n)
            result.append({'issue': issue, 'kind': kind, 'not_before': not_before,
                           'rank': rank})
        return sorted(result, key=lambda item: item['rank'])

    def worker_slots_used(self):
        return self.s.state.db.execute(
            "SELECT COUNT(*) FROM admission_leases WHERE owner LIKE 'job:%'").fetchone()[0]

    def background_allowed(self, issues, role):
        if self.demand(issues) or self.s.state.jobs(("working", "pr-open")):
            return False
        groups = self.s.admission.summary()['groups']
        names = self.s.admission.group_names(self.s.models[0])
        # Planning may supply an empty work frontier. Optional review cannot
        # claim a recovery probe merely because all workers are unavailable.
        return role == 'planner' or all(groups.get(n, {}).get('mode') == 'normal' for n in names)

    def pull(self, issues):
        s = self.s
        if s.state.paused():
            return
        s.refresh_improvements(issues)
        for request in self.demand(issues):
            n = request['issue']['number']
            if s.clock() < request['not_before']:
                continue
            if self.worker_slots_used() >= self.config['worker_slots']:
                break
            active = s.state.jobs(('working', 'pr-open'))
            if request['kind'] == 'repair':
                job = s.state.job(n)
                pending = s.state.get('repair:' + str(n))
                clone = job.get('clone') or job['worker']
                if not s.admission.reserve('job:' + str(n), s.model_for(job['worker']),
                                           Path(clone).name, s.state.capacity()):
                    continue
                if s.state.repair(n, pending['cause'], detail=pending['detail']):
                    s.state.set('repair:' + str(n), None)
                    s.state.set('delivery:' + str(n), None)
                    try:
                        s.launch(s.state.job(n), request['issue'], pending['reason'])
                    except Exception as exc:
                        s.block(s.state.job(n), str(exc))
                else:
                    s.admission.release('job:' + str(n))
                    s.state.set('repair:' + str(n), None)
                    s.block(job, 'Repair limit exhausted: ' + pending['reason'])
            elif len(active) < self.config['workspace_slots']:
                if request['kind'] == 'retry':
                    s.retries(issues, only=n)
                else:
                    s.dispatch(issues, only=n)

    def advance(self, issues, prs):
        """Observe completion, deliver results, then arbitrate inference once."""
        s = self.s
        errors = {}
        def phase(name, action):
            try:
                action()
            except Exception as exc:
                errors[name] = type(exc).__name__ + ': ' + str(exc)[:500]
                s.log('Factory ' + name + ' deferred: ' + errors[name])
        s.state.set('factory:inventory', issues)
        phase('workers', lambda: s.reconcile_workers(issues))
        for job in s.state.jobs(('pr-open',)):
            phase('delivery:' + str(job['issue']), lambda n=job['issue']: s.integrate(issues, only=n))
        phase('review', lambda: s.review(issues, prs, allow_launch=False))
        phase('planner', lambda: s.planner(issues, prs, allow_launch=False))
        phase('work', lambda: self.pull(issues))
        phase('background-planner', lambda: s.planner(issues, prs,
              allow_launch=self.background_allowed(issues, 'planner')))
        phase('background-review', lambda: s.review(issues, prs,
              allow_launch=self.background_allowed(issues, 'reviewer')))
        phase('mirror', lambda: s.mirror(issues))
        phase('qa', lambda: s.qa(issues))
        for job in s.state.jobs(('working',)):
            if s.state.get('delivery:' + str(job['issue'])) and job.get('error'):
                errors['publication:' + str(job['issue'])] = job['error']
        now = s.clock()
        demand = self.demand(issues)
        merged = s.state.db.execute("SELECT COUNT(*) FROM jobs WHERE status='done' AND updated>=?",
                                   (now - 86400,)).fetchone()[0]
        s.state.set('factory:status', {
            'policy': 'completion-first', 'observed_at': now,
            'worker_slots': self.config['worker_slots'],
            'worker_inference_active': self.worker_slots_used(),
            'workspace_slots': self.config['workspace_slots'],
            'delivery_backlog': len(s.state.jobs(('pr-open',))) + sum(
                bool(s.state.get('delivery:' + str(j['issue']))) for j in s.state.jobs(('working',))),
            'waiting_work': [{'issue': r['issue']['number'], 'kind': r['kind'],
                              'not_before': r['not_before']} for r in demand],
            'merged_last_24h': merged, 'daily_merge_goal': self.config['daily_merge_goal'],
            'errors': errors})
        s.state.set('last_error', '; '.join(errors.values()) or None)

    def offline(self):
        """Reconcile local receipts with cached task data, without new launches.

        This path is only for a failed inventory/API cycle. Publication may
        still fail, but completion releases inference and preserves artifacts.
        Stale inventory never authorizes a new assignment or merge.
        """
        issues = self.s.state.get('factory:inventory') or []
        if not issues:
            return
        try:
            self.s.reconcile_workers(issues)
        except Exception as exc:
            self.s.log('Factory offline receipt reconciliation deferred: ' + str(exc)[:500])
