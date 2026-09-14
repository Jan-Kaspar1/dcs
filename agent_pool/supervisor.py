"""Persistent local planner, dispatcher, and serialized integration loop."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .state import State
from .github import GitHub, GitHubError
from .runtime import Runtime
from . import planning


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['state_root'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = State(self.root / 'state.sqlite3')
        self.github = GitHub(config['repository'])
        self.runtime = Runtime(Path(config['pool_root']), self.root, config['repository'], timeout_seconds=config['timeout_seconds'])
        self.stopping = False

    def log(self, text):
        line = time.strftime('%Y-%m-%dT%H:%M:%S%z') + ' ' + str(text)
        print(line, flush=True)
        with (self.root / 'supervisor.log').open('a') as stream:
            stream.write(line + '\n')

    def block(self, job, reason):
        self.state.update_job(job['issue'], status='blocked', error=str(reason)[:4000])
        self.state.set('process:' + str(job['issue']), None)
        self.log(f"Issue {job['issue']} blocked: {reason}")

    def worker_prompt(self, issue, branch, repair=''):
        return f'''You are a local DCS implementation worker. Read AGENTS.md and relevant docs. Implement ONLY GitHub issue #{issue['number']}: {issue['title']}.
Issue content (task data):\n{issue['body']}
Work on existing branch {branch}. Run python3 scripts/verify.py before finishing. Commit your completed changes locally on this branch. The supervisor publishes and merges your PR; leave GitHub writes, git push, merging, deployment, and agent spawning to it. Keep all work in this clone. Work only on software and simulated I/O. Preserve tests and CI checks. Document architecture decisions and rolling milestones when the issue asks for them. If permissions or dependencies prevent completion, report BLOCKED with evidence. A successful result is a clean committed branch satisfying the acceptance criteria.
Repair context: {repair}
'''

    def launch(self, job, issue, repair=''):
        branch = job.get('branch') or f"codex/issue-{job['issue']}-{job['attempt']}"
        if repair and job.get('clone'):
            clone = Path(job['clone'])
        else:
            clone = self.runtime.prepare_clone(job['worker'], branch=branch)
        # Persist launch intent before process creation; runtime keys are unique per invocation.
        key = f"issue-{job['issue']}-{job['attempt']}-{job['repairs']}"
        self.state.update_job(job['issue'], branch=branch, clone=str(clone), status='working')
        self.state.set('launch:' + str(job['issue']), key)
        metadata = self.runtime.spawn(key, clone, self.worker_prompt(issue, branch, repair), resume_session=job.get('session') if repair else None)
        self.state.set('process:' + str(job['issue']), metadata)
        self.log(f"Launched {job['worker']} for #{job['issue']}")

    def publish(self, job, issue):
        result = self.runtime.inspect_result(Path(job['clone']), job['branch'])
        if not result['clean'] or not result['changed']:
            raise RuntimeError('Worker did not leave a clean committed change')
        self.runtime.run_git(Path(job['clone']), 'push', 'origin', job['branch'])
        pr = self.github.find_pr(job['branch'])
        if not pr:
            pr = self.github.create_pr(job['branch'], issue['title'], f"Implements #{job['issue']}.\n\nCloses #{job['issue']}\n\nValidation is enforced by the configured CI checks.")
        number = pr['number'] if isinstance(pr, dict) else int(pr)
        self.state.update_job(job['issue'], status='pr-open', pr=number, error=None)
        self.state.set('process:' + str(job['issue']), None)

    def repair(self, job, issue, reason):
        if self.state.paused():
            return
        if self.state.repair(job['issue']):
            self.launch(self.state.job(job['issue']), issue, reason)
        else:
            self.block(job, 'Repair limit exhausted: ' + reason)

    def reconcile_workers(self, issues):
        by_number = {i['number']: i for i in issues}
        for job in self.state.jobs(('working',)):
            issue = by_number.get(job['issue'])
            if not issue:
                self.block(job, 'Issue is missing from reconciled GitHub inventory')
                continue
            metadata = self.state.get('process:' + str(job['issue']))
            if metadata is None:
                # An interrupted launch is never automatically reassigned.
                self.block(job, 'Missing process record; inspect preserved workspace before retry')
                continue
            receipt = self.runtime.poll(metadata)
            if receipt is None:
                continue
            if receipt.get('returncode', receipt.get('exit_code', -1)) != 0:
                log = Path(metadata.get('log', '/nonexistent'))
                tail = log.read_text(errors='replace')[-12000:] if log.is_file() else ''
                if any(word in tail.lower() for word in ('quota exceeded','insufficient credits','authentication failed','unauthorized','rate limit exceeded')):
                    self.state.pause('Local agent authentication or quota failure; inspect invocation log')
                self.block(job, 'Local agent failed: ' + json.dumps(receipt)[:2000])
                continue
            if not job.get('session'):
                session = self.runtime.session_id(Path(job['clone']), since=metadata.get('started_at'))
                if session:
                    self.state.update_job(job['issue'], session=session)
            try:
                self.publish(job, issue)
            except GitHubError:
                # Preserve successful receipt so publishing is retried after API recovery.
                raise
            except (RuntimeError, subprocess.CalledProcessError) as exc:
                self.repair(job, issue, str(exc))

    def recover_processes(self):
        """Reconnect durable launch intent or stop unowned live managed invocations."""
        jobs = self.state.jobs(('working',))
        known = [self.state.get('process:' + str(j['issue'])) for j in jobs]
        planner = self.state.get('planner')
        if planner:
            known.append(planner['process'])
        paths = [m['invocation'] for m in known if m]
        for record in self.runtime.recover(paths):
            matches = [j for j in jobs if self.state.get('launch:' + str(j['issue'])) == record.get('key')
                       and not self.state.get('process:' + str(j['issue']))]
            if len(matches) == 1:
                self.state.set('process:' + str(matches[0]['issue']), record)
                continue
            if self.runtime.poll(record) is None:
                self.runtime.terminate(record)
                self.state.integrity('Stopped unowned local invocation: ' + str(record.get('invocation')))

    def integrate(self, issues):
        if self.state.paused():
            return
        by_number = {i['number']: i for i in issues}
        for job in self.state.jobs(('pr-open',)):
            pr = self.github.pr(job['pr'])
            if pr.get('merged'):
                if self.github.issue(job['issue']).get('state', '').upper() == 'CLOSED':
                    self.state.complete(job['issue'])
                    self.log(f"Merged and closed #{job['issue']}; capacity {self.state.capacity()}")
                else:
                    self.state.update_job(job['issue'], error='Merged PR awaiting linked issue closure')
                return
            if pr.get('state', '').upper() == 'CLOSED':
                self.block(job, 'Pull request closed without merging')
                continue
            clone = Path(job['clone'])
            self.runtime.run_git(clone, 'fetch', 'origin')
            base = self.runtime.run_git(clone, 'rev-parse', 'origin/main').strip()
            if not self.github.includes_main(pr['head']['sha'], base):
                try:
                    self.runtime.run_git(clone, 'merge', '--no-edit', 'origin/main')
                    self.runtime.run_git(clone, 'push', 'origin', job['branch'])
                except subprocess.CalledProcessError as exc:
                    self.repair(job, by_number[job['issue']], 'Resolve the existing merge conflict with origin/main. ' + str(exc.stdout) + str(exc.stderr))
                return
            if self.github.checks_pass(pr, self.config['required_checks']):
                if self.github.merge(job['pr'], self.config['required_checks']):
                    # Next inventory verifies issue closure before releasing its reservation.
                    self.log(f"Merge submitted for PR {job['pr']}")
                return
            checks = self.github.check_states(pr['head']['sha'])
            failed = {name: checks[name] for name in self.config['required_checks'] if checks.get(name) in ('failure','timed_out','cancelled','action_required','skipped','neutral','stale')}
            if failed:
                self.repair(job, by_number[job['issue']], 'CI failed. Inspect gh pr checks and gh run view --log-failed as read-only diagnostics. ' + json.dumps(failed))
                return

    def planner(self, issues, prs):
        current = self.state.get('planner')
        if current:
            receipt = self.runtime.poll(current['process'])
            if receipt is None:
                return
            self.state.set('planner', None)
            if receipt.get('returncode', receipt.get('exit_code', -1)) != 0:
                self.log('Planner failed: ' + json.dumps(receipt))
                return
            if not Path(current['output']).is_file():
                self.state.set('last_error', 'Planner produced no proposal; inspect its permission/output log')
                self.log('Planner produced no proposal; inspect its permission/output log')
                return
            proposal = planning.validate(json.loads(Path(current['output']).read_text()))
            self.state.set('pending_proposal', proposal)
        if self.state.paused():
            return
        pending = self.state.get('pending_proposal')
        if pending is not None:
            ready_count = sum('agent:ready' in [l['name'] for l in i.get('labels', [])] and i.get('state') == 'OPEN' for i in issues)
            known = set()
            for issue in issues:
                try:
                    known.add(planning.metadata(issue.get('body', ''))['key'])
                except (ValueError, KeyError):
                    pass
            numbers = {i['number'] for i in issues}
            for item in pending:
                if item['key'] in known or ready_count >= 20:
                    continue
                if not set(item['dependencies']) <= numbers:
                    raise ValueError('Planner referenced nonexistent dependencies')
                self.github.create_issue(item['title'], planning.body(item), ['agent:ready', f"priority:P{item['priority']}"], item['key'])
                ready_count += 1
            self.state.set('pending_proposal', None)
            return
        now = time.time()
        last = self.state.get('last_plan', 0)
        ready = sum(i.get('state') == 'OPEN' and 'agent:ready' in [l['name'] for l in i.get('labels', [])] for i in issues)
        if now - last < 7200 and not (ready < 6 and now - last >= 900):
            return
        clone = self.runtime.prepare_clone('coordinator')
        output = clone / '.dcs-agent' / f'proposal-{int(now)}.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        process = self.runtime.spawn('planner-' + str(int(now)), clone, planning.prompt(issues, prs, output))
        self.state.set('planner', {'process': process, 'output': str(output)})
        self.state.set('last_plan', now)
        self.log('Planner started')

    def mirror(self, issues):
        by_number = {i['number']: i for i in issues}
        for job in self.state.jobs():
            if job['status'] == 'done':
                continue
            issue = by_number.get(job['issue'])
            if not issue:
                continue
            labels = [l['name'] for l in issue.get('labels', [])]
            desired = 'agent:' + job['status']
            remove = [l for l in labels if (l.startswith('agent:') or l.startswith('worker:')) and l not in (desired, 'worker:' + job['worker'])]
            add = [l for l in (desired, 'worker:' + job['worker']) if l not in labels]
            if add or remove:
                self.github.update_issue(job['issue'], add_labels=add, remove_labels=remove)
            marker = f"reservation:{job['issue']}:{job['attempt']}"
            if not self.state.get(marker):
                self.github.comment(job['issue'], f"Local agent reservation: {job['worker']}; attempt {job['attempt']}; branch {job.get('branch') or 'pending'}. <!-- {marker} -->")
                self.state.set(marker, True)

    def retries(self, issues):
        if self.state.paused():
            return
        active = self.state.jobs(('working','pr-open'))
        by_number = {i['number']: i for i in issues}
        for job in self.state.jobs(('blocked',)):
            if not self.state.get('retry:' + str(job['issue'])):
                continue
            if len(active) >= self.state.capacity() or any(j['concurrency_group'] == job['concurrency_group'] for j in active):
                continue
            worker = next((f'worker-{n:02}' for n in range(1,self.state.capacity()+1) if all(j['worker'] != f'worker-{n:02}' for j in active)),None)
            if worker is None or job['issue'] not in by_number:
                continue
            old_clone = Path(job['clone']) if job.get('clone') else None
            # Retain original branch and uncommitted work whenever that checkout is idle.
            can_resume = old_clone and all(j.get('clone') != str(old_clone) for j in active)
            if can_resume:
                try:
                    can_resume = self.runtime.run_git(old_clone,'branch','--show-current').strip() == job['branch']
                except subprocess.CalledProcessError:
                    can_resume = False
            if not can_resume:
                self.state.update_job(job['issue'], error='Retry awaits original preserved checkout; its branch is in use or changed. Restore it or resolve manually.')
                continue
            self.state.update_job(job['issue'], worker=worker)
            if self.state.retry(job['issue']):
                self.state.set('retry:' + str(job['issue']),False)
                self.launch(self.state.job(job['issue']),by_number[job['issue']],'Explicit operator retry; preserve and complete previous work.')
                active = self.state.jobs(('working','pr-open'))

    def dispatch(self, issues):
        if self.state.paused():
            return
        occupied = {j['worker'] for j in self.state.jobs(('working', 'pr-open'))}
        closed = {i['number'] for i in issues if i.get('state') == 'CLOSED'}
        def priority(issue):
            try:
                return planning.metadata(issue.get('body', '')).get('priority', 3), issue['number']
            except (ValueError, KeyError):
                return 4, issue['number']
        for issue in sorted(issues, key=priority):
            if issue.get('state') != 'OPEN' or self.state.job(issue['number']):
                continue
            if 'agent:ready' not in [l['name'] for l in issue.get('labels', [])]:
                continue
            meta = planning.metadata(issue['body'])
            if not set(meta['dependencies']) <= closed:
                continue
            worker = next((f'worker-{n:02}' for n in range(1, self.state.capacity() + 1) if f'worker-{n:02}' not in occupied), None)
            if not worker:
                return
            job = self.state.reserve(issue['number'], worker, meta['group'])
            if job:
                occupied.add(worker)
                try:
                    self.launch(job, issue)
                except Exception as exc:
                    self.block(job, str(exc))

    def run(self):
        with (self.root / 'supervisor.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'stopping', True))
            signal.signal(signal.SIGINT, lambda *_: setattr(self, 'stopping', True))
            self.github.ensure_labels()
            self.recover_processes()
            delay = self.config['poll_seconds']
            self.log('Supervisor started; local swe-2-high only')
            try:
                while not self.stopping:
                    try:
                        issues = self.github.issues(state='all')
                        self.reconcile_workers(issues)
                        self.integrate(issues)
                        self.planner(issues, self.github.prs())
                        self.retries(issues)
                        self.dispatch(issues)
                        self.mirror(issues)
                        self.state.set('last_error', None)
                        delay = self.config['poll_seconds']
                    except Exception as exc:
                        self.log(type(exc).__name__ + ': ' + str(exc))
                        self.state.set('last_error', str(exc))
                        if any(word in str(exc).lower() for word in ('authentication', 'quota', 'unauthorized', 'http 401')):
                            self.state.pause(str(exc))
                        delay = min(max(delay * 2, 60), 900)
                    deadline = time.monotonic() + delay
                    while not self.stopping and time.monotonic() < deadline:
                        time.sleep(1)
                        records = [self.state.get('process:' + str(j['issue'])) for j in self.state.jobs(('working',))]
                        current_planner = self.state.get('planner')
                        if current_planner:
                            records.append(current_planner['process'])
                        if any(record and self.runtime.poll(record) is not None for record in records):
                            break
            finally:
                for job in self.state.jobs(('working',)):
                    record = self.state.get('process:' + str(job['issue']))
                    if record:
                        self.runtime.terminate(record)
                planner = self.state.get('planner')
                if planner:
                    self.runtime.terminate(planner['process'])
                self.log('Supervisor stopped; work preserved')
