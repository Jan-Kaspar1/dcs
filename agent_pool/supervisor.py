"""Persistent local planner, dispatcher, and serialized integration loop."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

from .state import State, REDISPATCH_CAUSES
from .admission import Admission, classify
from .github import GitHub, GitHubError
from .runtime import Runtime
from . import areas
from . import findings as findings_lane
from . import planning
from . import scheduling
from . import review as review_lane


class RecoveryUncertain(RuntimeError):
    """Recovery could not prove whether preserved work exists; never implies none."""


class AdmissionDenied(RuntimeError):
    """Inference admission was refused immediately before a launch."""


CONFLICT_PATH = re.compile(r'^CONFLICT \([^)]*\): Merge conflict in (.+)$', re.M)


def conflict_paths(*outputs):
    """Conflicted paths git's failed-merge output reports, in first-seen order."""
    paths = []
    for text in outputs:
        for match in CONFLICT_PATH.finditer(str(text or '')):
            path = match.group(1).strip()
            if path:
                paths.append(path)
    return list(dict.fromkeys(paths))


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['state_root'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = State(self.root / 'state.sqlite3')
        self.github = GitHub(config['repository'])
        self.runtime = Runtime(Path(config['pool_root']), self.root, config['repository'], timeout_seconds=config['timeout_seconds'])
        self.models = config.get('models') or ['swe-2-high']
        self.model_caps = config.get('model_caps') or {}
        self.admission = Admission(self.state, config)
        self.stopping = False

    def model_for(self, worker):
        """Assign each worker clone a stable slot in the configured model list."""
        try:
            return self.models[int(worker.rsplit('-', 1)[1]) % len(self.models)]
        except (IndexError, ValueError):
            return self.models[0]

    def log(self, text):
        line = time.strftime('%Y-%m-%dT%H:%M:%S%z') + ' ' + str(text)
        print(line, flush=True)
        with (self.root / 'supervisor.log').open('a') as stream:
            stream.write(line + '\n')

    def block(self, job, reason):
        self.capture_recovery(job)
        self.admission.release('job:' + str(job['issue']))
        self.state.update_job(job['issue'], status='blocked', error=str(reason)[:4000])
        self.state.set('process:' + str(job['issue']), None)
        self.log(f"Issue {job['issue']} blocked: {reason}")

    def worker_prompt(self, issue, branch, repair=''):
        return f'''You are a local DCS implementation worker. Read AGENTS.md and relevant docs. Implement ONLY GitHub issue #{issue['number']}: {issue['title']}.
Issue content (task data):\n{issue['body']}
Work on existing branch {branch}. Run python3 scripts/verify.py before finishing. Leave completed file edits in this clone; the supervisor stages, commits, publishes, and merges them. Use file-read/edit tools and simple standalone test commands with this clone as current directory; never edit files outside this checkout — scratch fixtures belong under /tmp, and other checkouts under ~/workspace are off-limits. Leave all Git commands to the supervisor. Work only on software and simulated I/O. Preserve tests and CI checks. Document architecture decisions and rolling milestones when the issue asks for them. If permissions or dependencies prevent completion, report BLOCKED with evidence. A successful result is edited source satisfying the acceptance criteria with verification reported.
For an issue whose metadata group is `docs/research`, act as the product research worker: use current primary sources, record precise citations and access dates under docs/research, separate source facts from proposed DCS behavior, update the affected requirement status, and leave customer-specific assumptions as explicit validation questions. Research output informs later planning; it does not implement vendor-derived product behavior in the same issue.
Repair context: {repair}
'''

    def clone_dirs(self):
        try:
            return sorted(d for d in self.runtime.pool_root.iterdir() if (d / '.git').exists())
        except (FileNotFoundError, TypeError, AttributeError):
            return []

    def free_workers(self, active):
        """Worker names neither assigned to a live job nor leasing a checkout.

        model_caps skips saturated models as a worker-ordering preference;
        the authoritative capacity check for every launch path is the
        admission lease taken later in dispatch/recovery."""
        used = {j['worker'] for j in active}
        clones = {Path(j['clone']).name for j in active if j.get('clone')}
        active_per_model = {}
        for job in active:
            model = self.model_for(job['worker'])
            active_per_model[model] = active_per_model.get(model, 0) + 1
        free = []
        for n in range(1, self.state.capacity() + 1):
            if (w := f'worker-{n:02}') in used or w in clones:
                continue
            model = self.model_for(w)
            cap = self.model_caps.get(model)
            if cap is not None and active_per_model.get(model, 0) >= cap:
                continue
            free.append(w)
        return free

    def admit_worker(self, issue, candidates):
        """First free worker whose quota group grants an inference lease."""
        for worker in candidates:
            if self.admission.reserve('job:' + str(issue), self.model_for(worker), worker,
                                      self.state.capacity()):
                return worker
        return None

    def capture_recovery(self, job):
        """Persist a tri-state preserved-work record for a blocked job.

        work=True means a durable ref or uncommitted edits exist; work=False is
        asserted only after every checkout, quarantine, and the remote were
        surveyed; work=None means the survey could not prove either way.
        """
        issue = job['issue']
        previous = self.state.get('recovery:' + str(issue)) or {}
        rec = {'branch': job.get('branch'), 'clone': job.get('clone'), 'head': None,
               'basis': 'unknown', 'work': None, 'detail': '', 'phase': 'captured',
               'attempts': previous.get('attempts', 0),
               'quota_requeues': previous.get('quota_requeues', 0), 'updated': time.time()}
        try:
            if not rec['branch']:
                rec.update(basis='no-branch', work=False,
                           detail='job was blocked before a branch existed')
            else:
                self.survey_preserved(job, rec)
        except Exception as exc:
            rec.update(basis='unknown', work=None, detail=str(exc)[:2000])
        self.state.set('recovery:' + str(issue), rec)
        return rec

    def survey_preserved(self, job, rec):
        branch = rec['branch']
        sources, errors = [], []
        candidates = self.clone_dirs()
        if rec.get('clone'):
            recorded = str(Path(rec['clone']).resolve())
            candidates.sort(key=lambda d: 0 if str(d.resolve()) == recorded else 1)
        for directory in candidates:
            try:
                head = self.runtime.run_git(directory, 'rev-parse', '--verify', branch)
            except subprocess.CalledProcessError:
                continue
            except Exception as exc:
                errors.append(f'{directory.name}: {exc}')
                continue
            if not head:
                continue
            source = {'clone': str(directory), 'head': head, 'dirty': False}
            try:
                if self.runtime.run_git(directory, 'branch', '--show-current') == branch:
                    source['dirty'] = bool(self.runtime.run_git(directory, 'status', '--porcelain'))
            except Exception:
                pass
            sources.append(source)
        remote_head, remote_error = None, None
        if candidates:
            probe = (candidates[0], 'ls-remote', 'origin', 'refs/heads/' + branch)
        else:
            source = self.runtime.repository
            if not source.startswith(('/', 'https://', 'git@', 'file://')):
                source = 'https://github.com/' + source + '.git'
            probe = (self.root, 'ls-remote', source, 'refs/heads/' + branch)
        try:
            out = self.runtime.run_git(*probe)
            remote_head = out.split()[0] if out else None
        except Exception as exc:
            remote_error = str(exc)
        for source in sources:
            if source['dirty'] and not self.runtime.clone_owned(Path(source['clone'])):
                try:
                    self.runtime.run_git(source['clone'], 'add', '--all')
                    self.runtime.run_git(source['clone'], 'commit', '-m',
                                         'WIP: preserve interrupted work for issue #' + str(job['issue']))
                    source['head'] = self.runtime.run_git(source['clone'], 'rev-parse', branch)
                    source['wip'], source['dirty'] = True, False
                except Exception as exc:
                    errors.append('wip commit failed: ' + str(exc))
        rec['sources'] = sources
        if remote_head:
            rec['remote_head'] = remote_head
        if any(s.get('wip') or s['dirty'] for s in sources):
            rec.update(work=True, basis='uncommitted', head=sources[0]['head'])
        elif sources or remote_head:
            where = sources[0]['clone'] if sources else str(candidates[0])
            head = sources[0]['head'] if sources else remote_head
            try:
                changed = self.runtime.run_git(where, 'diff', '--name-only', 'origin/main...' + head)
                rec.update(work=bool(changed), basis='committed' if changed else 'clean-ref', head=head)
            except Exception as exc:
                rec.update(work=True, basis='ref-unverified', head=head,
                           detail='could not diff against origin/main: ' + str(exc)[:500])
        elif errors or remote_error:
            rec.update(work=None, basis='unknown',
                       detail='; '.join(errors + ([remote_error] if remote_error else []))[:2000])
        else:
            rec.update(work=False, basis='absent',
                       detail='no ref for ' + branch + ' in ' + str(len(candidates)) + ' checkouts or origin')

    def restore_preserved(self, job, rec, target, worker):
        """Put target checkout onto the job branch with its preserved head."""
        branch = rec['branch']
        orig = Path(rec['clone']) if rec.get('clone') else None
        if orig is not None and target == orig and target.is_dir():
            current = self.runtime.run_git(target, 'branch', '--show-current')
            dirty = self.runtime.run_git(target, 'status', '--porcelain')
            if current != branch:
                if dirty:
                    raise RecoveryUncertain(target.name + ' is dirty on ' + current + '; refusing to switch')
                try:
                    self.runtime.run_git(target, 'rev-parse', '--verify', branch)
                    self.runtime.run_git(target, 'switch', branch)
                except subprocess.CalledProcessError:
                    if rec.get('head'):
                        self.runtime.run_git(target, 'switch', '-c', branch, rec['head'])
                    else:
                        raise RecoveryUncertain('branch ' + branch + ' missing from ' + target.name)
            elif dirty:
                self.runtime.run_git(target, 'add', '--all')
                self.runtime.run_git(target, 'commit', '-m',
                                     'WIP: preserve interrupted work for issue #' + str(job['issue']))
            head = self.runtime.run_git(target, 'rev-parse', 'HEAD')
            if rec.get('head') and head != rec['head']:
                try:
                    self.runtime.run_git(target, 'merge-base', '--is-ancestor', rec['head'], 'HEAD')
                except subprocess.CalledProcessError:
                    self.runtime.run_git(target, 'reset', '--hard', rec['head'])
                    head = rec['head']
            return head
        self.runtime.prepare_clone(worker)
        ref = '+refs/heads/' + branch + ':refs/heads/' + branch
        attempts = [s['clone'] for s in rec.get('sources', [])] + ['origin']
        errors = []
        for source in attempts:
            try:
                self.runtime.run_git(target, 'fetch', source, ref)
                break
            except subprocess.CalledProcessError as exc:
                errors.append(str(source) + ': ' + str(exc.stderr or exc))
        else:
            raise RecoveryUncertain('no reachable ref for ' + branch + '; tried ' + '; '.join(errors)[:1000])
        self.runtime.run_git(target, 'switch', branch)
        head = self.runtime.run_git(target, 'rev-parse', 'HEAD')
        if rec.get('head') and head != rec['head']:
            try:
                self.runtime.run_git(target, 'reset', '--hard', rec['head'])
                head = rec['head']
            except subprocess.CalledProcessError:
                raise RecoveryUncertain('recorded head ' + rec['head'][:8] + ' not reachable from fetched ' + branch)
        return head

    def fresh_start(self, job, rec, target, worker):
        """Create the job branch from current main after proving no work exists."""
        self.runtime.prepare_clone(worker)
        try:
            self.runtime.run_git(target, 'switch', '-c', rec['branch'], 'origin/main')
        except subprocess.CalledProcessError:
            try:
                changed = self.runtime.run_git(target, 'diff', '--name-only', 'origin/main...' + rec['branch'])
            except subprocess.CalledProcessError as exc:
                raise RecoveryUncertain('cannot verify leftover ref ' + rec['branch'] + ': ' + str(exc))
            if changed:
                raise RecoveryUncertain('leftover ref ' + rec['branch'] + ' carries work; refusing fresh start')
            self.runtime.run_git(target, 'switch', '-C', rec['branch'], 'origin/main')

    def recover_job(self, job, rec, active, issue):
        """Restore or recreate the preserved workspace, then relaunch once."""
        number = job['issue']
        if rec.get('work') is None:
            detail = rec.get('detail') or 'preserved-work state could not be established'
            self.state.update_job(number, error='Recovery uncertain: ' + detail)
            if rec.get('phase') != 'error':
                self.log('Retry #' + str(number) + ': recovery uncertain - ' + detail)
            rec['phase'] = 'error'
            self.state.set('recovery:' + str(number), rec)
            return False
        if not rec.get('branch'):
            rec['branch'] = job.get('branch') or 'codex/issue-' + str(number) + '-' + str(job['attempt'])
        leased = {str(Path(j['clone']).resolve()) for j in active if j.get('clone')}
        free = self.free_workers(active)
        orig = Path(rec['clone']) if rec.get('clone') else None
        owner = 'job:' + str(number)
        target = worker = None
        if (rec['work'] and orig is not None and re.fullmatch(r'worker-\d+', orig.name)
                and orig.is_dir() and str(orig.resolve()) not in leased
                and self.admission.reserve(owner, self.model_for(orig.name), orig.name,
                                           self.state.capacity())):
            target, worker = orig, orig.name
        elif free:
            worker = self.admit_worker(number, free)
            if worker:
                target = self.runtime.pool_root / worker
        if worker is None:
            if rec.get('phase') != 'waiting':
                self.log('Retry #' + str(number) + ': waiting for a free clone or inference slot')
            rec['phase'] = 'waiting'
            self.state.set('recovery:' + str(number), rec)
            return False
        rec.update(phase='leased', target_clone=str(target), worker=worker)
        self.state.set('recovery:' + str(number), rec)
        try:
            if rec['work']:
                head = self.restore_preserved(job, rec, target, worker)
                repair = 'Retry after recovery: preserved work restored at ' + head[:8] + ' on ' + rec['branch'] + '; continue and complete it.'
            else:
                self.fresh_start(job, rec, target, worker)
                repair = 'Retry after recovery: no preserved work existed; starting from current main.'
        except Exception as exc:
            self.admission.release(owner)
            rec.update(phase='error', attempts=rec.get('attempts', 0) + 1, detail=str(exc)[:2000])
            self.state.set('recovery:' + str(number), rec)
            if rec['attempts'] >= 3:
                self.state.set('retry:' + str(number), False)
            self.state.update_job(number, error='Recovery failed: ' + str(exc)[:2000])
            self.log('Retry #' + str(number) + ': recovery failed - ' + str(exc)[:500])
            return False
        self.state.update_job(number, worker=worker, clone=str(target))
        # The retry flag carries the redispatch class when a call site armed
        # it (requeue_quota); an operator-armed retry defaults to the generic
        # worker-failure class. jobs.error keeps the free-text detail.
        cause = self.state.get('retry:' + str(number))
        if cause not in REDISPATCH_CAUSES:
            cause = 'worker-failure'
        if not self.state.retry(number, cause):
            self.admission.release(owner)
            self.state.update_job(number, error='Retry rejected: repair budget exhausted')
            return False
        try:
            self.launch(self.state.job(number), issue, repair)
        except Exception as exc:
            self.log('Retry #' + str(number) + ': launch after recovery failed - ' + str(exc))
            return False
        rec.update(phase='done', target_clone=str(target))
        self.state.set('recovery:' + str(number), rec)
        self.state.set('retry:' + str(number), False)
        outcome = 'restored preserved work' if rec['work'] else 'fresh start'
        self.log('Retry #' + str(number) + ': ' + outcome + '; relaunched on ' + worker)
        return True

    def launch(self, job, issue, repair=''):
        branch = job.get('branch') or f"codex/issue-{job['issue']}-{job['attempt']}"
        owner = 'job:' + str(job['issue'])
        model = self.model_for(job['worker'])
        preserved = Path(job['clone']) if repair and job.get('clone') else None
        # Admission is idempotent per owner: callers may reserve earlier to pick
        # a worker, but every launch path must hold an inference lease here.
        if not self.admission.reserve(owner, model, (preserved or Path(job['worker'])).name,
                                      self.state.capacity()):
            raise AdmissionDenied('Inference admission denied for ' + owner)
        try:
            clone = preserved if preserved else self.runtime.prepare_clone(job['worker'], branch=branch)
            # Persist launch intent before process creation; runtime keys are unique per invocation.
            key = f"issue-{job['issue']}-{job['attempt']}-{job['repairs']}"
            self.state.update_job(job['issue'], branch=branch, clone=str(clone), status='working')
            self.state.set('launch:' + str(job['issue']), key)
            metadata = self.runtime.spawn(key, clone, self.worker_prompt(issue, branch, repair), resume_session=job.get('session') if repair else None, model=model)
            self.state.set('process:' + str(job['issue']), metadata)
            self.admission.attach(owner, metadata)
        except Exception:
            self.admission.release(owner)
            raise
        self.log(f"Launched {job['worker']} for #{job['issue']} on {model}")

    def publish(self, job, issue):
        result = self.runtime.inspect_result(Path(job['clone']), job['branch'])
        if not result['clean']:
            self.runtime.run_git(Path(job['clone']), 'diff', '--check')
            self.runtime.run_git(Path(job['clone']), 'add', '--all')
            self.runtime.run_git(Path(job['clone']), 'commit', '-m', f"Implement issue #{job['issue']}: {issue['title']}")
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

    def repair(self, job, issue, reason, cause, detail=None):
        if self.state.paused():
            return
        owner = 'job:' + str(job['issue'])
        clone = Path(job['clone']).name if job.get('clone') else job['worker']
        if not self.admission.reserve(owner, self.model_for(job['worker']), clone,
                                      self.state.capacity()):
            self.log(f"Repair for #{job['issue']} deferred: inference admission denied")
            return
        if self.state.repair(job['issue'], cause, detail=detail):
            self.launch(self.state.job(job['issue']), issue, reason)
        else:
            self.admission.release(owner)
            self.block(job, 'Repair limit exhausted: ' + reason)

    def requeue_quota(self, job, category):
        """Arm one bounded retry for a quota-killed invocation.

        The retry flag is consumed when recovery relaunches, so one failed
        invocation can never spend more than one requeue; repeated quota
        deaths are bounded by scheduler.max_quota_requeues.
        """
        number = job['issue']
        rec = self.state.get('recovery:' + str(number)) or {}
        used = rec.get('quota_requeues', 0)
        if used >= self.admission.max_quota_requeues:
            self.log(f"#{number} quota requeue budget exhausted ({used}); leaving blocked")
            return
        rec['quota_requeues'] = used + 1
        self.state.set('recovery:' + str(number), rec)
        self.state.set('retry:' + str(number), 'quota-requeue')
        self.log(f"#{number} requeued after {category} failure "
                 f"({used + 1}/{self.admission.max_quota_requeues})")

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
            output = Path(metadata.get('log', '/nonexistent'))
            output_tail = output.read_text(errors='replace')[-16000:] if output.is_file() else ''
            owner = 'job:' + str(job['issue'])
            if 'rejected a tool call that requires confirmation' in output_tail or 'BLOCKED' in output_tail:
                self.admission.finish(owner, metadata, 'failure')
                self.block(job, 'Agent reported a blocker or smart-mode permission rejection; work preserved')
                continue
            if receipt.get('returncode', receipt.get('exit_code', -1)) != 0:
                log = Path(metadata.get('log', '/nonexistent'))
                tail = log.read_text(errors='replace')[-12000:] if log.is_file() else ''
                category, retry_after = classify(receipt, tail)
                self.admission.finish(owner, metadata, category, retry_after)
                if job.get('session') and 'session' in tail.lower() and (
                        'not found' in tail.lower() or 'no such' in tail.lower()):
                    # The stored session is gone; the next retry must start a
                    # fresh one instead of failing on --resume/--session again.
                    self.state.update_job(job['issue'], session=None)
                if category in ('rate', 'endpoint'):
                    self.requeue_quota(job, category)
                elif category in ('auth', 'credits'):
                    self.state.set('last_error', 'Local agent ' + category + ' failure; '
                                   'resolve it and run: dcs-agents admission reset <group>')
                self.block(job, 'Local agent failed: ' + json.dumps(receipt)[:2000])
                continue
            if not job.get('session'):
                session = self.runtime.session_id(Path(job['clone']), since=metadata.get('started_at'),
                                                  model=self.model_for(job['worker']),
                                                  key=metadata.get('key'))
                if session:
                    self.state.update_job(job['issue'], session=session)
            try:
                self.publish(job, issue)
                self.admission.finish(owner, metadata, 'success')
            except GitHubError:
                # Preserve successful receipt so publishing is retried after API recovery.
                raise
            except Exception as exc:
                # Any other publish-path failure (wrong branch, unclean result,
                # git or state error) belongs to this job alone; repair or block
                # it so the same pass still reaches the remaining jobs.
                self.repair(job, issue, str(exc), 'publish-error')

    def recover_processes(self):
        """Reconnect durable launch intent or stop unowned live managed invocations."""
        jobs = self.state.jobs(('working',))
        known = [self.state.get('process:' + str(j['issue'])) for j in jobs]
        planner = self.state.get('planner')
        if planner:
            known.append(planner['process'])
        reviewer = self.state.get('reviewer')
        if reviewer:
            known.append(reviewer['process'])
        paths = [m['invocation'] for m in known if m]
        for record in self.runtime.recover(paths):
            launch = self.state.get('reviewer:launch')
            if launch and record.get('key') == launch['key'] and not self.state.get('reviewer'):
                self.state.set('reviewer', {**launch, 'process': record})
                self.log('Recovered interrupted review launch ' + launch['run_id'])
                continue
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
                    self.admission.useful({'owner': 'job:' + str(job['issue'])})
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
                    paths = conflict_paths(exc.stdout, exc.stderr)
                    self.repair(job, by_number[job['issue']], 'Resolve the existing merge conflict with origin/main. ' + str(exc.stdout) + str(exc.stderr), 'merge-conflict', detail={'paths': paths} if paths else None)
                return
            if self.github.checks_pass(pr, self.config['required_checks']):
                if self.github.merge(job['pr'], self.config['required_checks']):
                    # Next inventory verifies issue closure before releasing its reservation.
                    self.log(f"Merge submitted for PR {job['pr']}")
                return
            checks = self.github.check_states(pr['head']['sha'])
            failed = {name: checks[name] for name in self.config['required_checks'] if checks.get(name) in ('failure','timed_out','cancelled','action_required','skipped','neutral','stale')}
            if failed:
                self.repair(job, by_number[job['issue']], 'CI failed. Inspect gh pr checks and gh run view --log-failed as read-only diagnostics. ' + json.dumps(failed), 'ci-failure', detail={'checks': sorted(failed)})
                return

    def review_stage(self, cfg=None):
        """Rollout stage: report -> pilot -> full, per config or persisted state."""
        cfg = cfg or review_lane.settings(self.config)
        if cfg['mode'] == 'full' or self.state.get('review:stage') == 'full':
            return 'full'
        if cfg['mode'] == 'pilot' or self.state.get('review:stage') == 'pilot':
            return 'pilot'
        return 'report'

    def slots_used(self, active=None):
        """Reserved agent slots: live jobs plus a running reviewer invocation."""
        if active is None:
            active = self.state.jobs(('working', 'pr-open'))
        return len(active) + (1 if self.state.get('reviewer') else 0)

    def new_failure_evidence(self):
        last = self.state.get('review:last_finished_at', 0)
        return any(j['status'] == 'blocked' and (j.get('updated') or 0) > last for j in self.state.jobs())

    def review_context(self, clone, sha, issues, prs, cfg):
        cursor = self.state.get('review:cursor', 0)
        try:
            commits = self.runtime.run_git(clone, 'log', '--oneline', '-30', sha).splitlines()
        except subprocess.CalledProcessError:
            commits = []
        open_issues = [{'number': i['number'], 'title': i['title'],
                        'labels': [l['name'] for l in i.get('labels', [])],
                        'body': (i.get('body') or '')[:1500]}
                       for i in issues if i.get('state') == 'OPEN'][:100]
        closed = sorted((i for i in issues if i.get('state') == 'CLOSED'),
                        key=lambda i: i['number'], reverse=True)[:30]
        missing = [p for p in ('AGENTS.md', 'CONTEXT.md', 'docs/architecture.md', 'docs/plan.md')
                   if not (clone / p).exists()]
        return {
            'base_sha': sha,
            'previous_reviewed_sha': self.state.last_completed_sha(),
            'coverage_focus': review_lane.AREAS[cursor % len(review_lane.AREAS)],
            'recent_commits': commits,
            'open_issues': open_issues,
            'open_prs': prs[:100],
            'recently_closed_issues': [{'number': i['number'], 'title': i['title']} for i in closed],
            'prior_dispositions': [{'key': c['key'], 'disposition': c['disposition'],
                                    'reason': c['reason'], 'revisit': c['revisit']}
                                   for c in self.state.candidates() if c['disposition'] != 'pending'][:50],
            'pending_candidates': [json.loads(c['payload']) for c in self.state.candidates('pending')],
            'pending_assessments': [{'key': c['key'], 'title': c['title'],
                                     'issues': self.state.improvement_issues(c['key']),
                                     'proposal': json.loads(c['payload'])}
                                    for c in self.state.pending_assessments()],
            'failure_evidence': [{'issue': j['issue'], 'error': (j.get('error') or '')[:1000]}
                                 for j in self.state.jobs(('blocked',))],
            'missing_context': missing,
            'issue_inventory_size': len(issues),
        }

    def launch_review(self, cfg, slot, issues, prs, manual=False, attempt=1):
        if not self.admission.reserve('reviewer', self.models[0], 'reviewer', self.state.capacity()):
            self.log('Review deferred: inference admission denied')
            return
        sha = self.github.main_sha()
        run_id = time.strftime('r%Y%m%d', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        self.state.begin_review(run_id, slot, sha, attempt)
        self.state.set('review:last_slot', slot)
        self.state.set('review:last_sha', sha)
        self.state.set('review:manual', False)
        if not manual and attempt == 1 and sha == self.state.last_completed_sha() and not self.new_failure_evidence():
            self.admission.release('reviewer')
            self.state.finish_review(run_id, 'skipped', report='base revision unchanged')
            self.log(f'Review {run_id} skipped: base {sha[:12]} unchanged')
            return
        try:
            clone = self.runtime.prepare_clone('reviewer')
            self.runtime.run_git(clone, 'fetch', 'origin')
            self.runtime.run_git(clone, 'switch', '--detach', sha)
            report_dir = self.root / 'reviews' / run_id
            report_dir.mkdir(parents=True, exist_ok=True)
            resource_dir = report_dir / 'resources'
            hashes = review_lane.stage_resources(resource_dir)
            template = (resource_dir / 'prompt.md').read_text()
        except (ValueError, RuntimeError, KeyError, OSError, subprocess.CalledProcessError) as exc:
            self.admission.release('reviewer')
            self.finish_review_run(run_id, slot, attempt, 'inconclusive',
                                   'Preflight failed: ' + str(exc)[:2000])
            self.state.set('last_error', 'Review preflight failed: ' + str(exc)[:500])
            return
        context = self.review_context(clone, sha, issues, prs, cfg)
        text = review_lane.prompt(template, run_id, sha, context['previous_reviewed_sha'],
                                  report_dir, resource_dir, context, cfg)
        key = 'review-' + run_id
        intent = {'key': key, 'run_id': run_id, 'slot': slot, 'base_sha': sha,
                  'report_dir': str(report_dir), 'clone': str(clone),
                  'attempt': attempt, 'hashes': hashes}
        self.state.set('reviewer:launch', intent)
        try:
            process = self.runtime.spawn(key, clone, text, timeout=cfg['timeout_seconds'], model=self.models[0])
        except Exception:
            self.admission.release('reviewer')
            raise
        self.admission.attach('reviewer', process)
        self.state.set('reviewer', {**intent, 'process': process})
        self.log(f'Review {run_id} launched at {sha[:12]} (attempt {attempt})')

    def finish_review_run(self, run_id, slot, attempt, status, error=None, report=None):
        self.state.finish_review(run_id, status, error=error, report=report)
        if status == 'inconclusive' and attempt == 1:
            self.state.set('review:retry_pending', slot)
        self.log(f'Review {run_id} {status}' + (f': {error}' if error else ''))

    def ingest_review(self, current, receipt, cfg, issues):
        run_id, slot, attempt = current['run_id'], current['slot'], current['attempt']
        report, error = None, None
        code = receipt.get('returncode', receipt.get('exit_code', -1))
        if receipt.get('status', 'completed') != 'completed' or code != 0:
            error = 'Reviewer invocation failed: ' + json.dumps(receipt)[:2000]
        else:
            path = Path(current['report_dir']) / 'report.json'
            if not path.is_file():
                error = 'Reviewer produced no report.json'
            else:
                try:
                    report = review_lane.validate_report(
                        path.read_text(errors='replace'), current['clone'], run_id,
                        current['base_sha'], known_issues={i['number'] for i in issues},
                        max_candidates=cfg['max_candidates'],
                        expected_hashes=current['hashes'])
                except ValueError as exc:
                    error = 'Invalid report: ' + str(exc)[:2000]
        if report is None:
            self.finish_review_run(run_id, slot, attempt, 'inconclusive', error)
            return
        status = report['status']
        skipped = {}
        # Only a completed review publishes candidates and assessments; an
        # inconclusive or skipped report is retained as evidence, never as
        # accepted review output.
        if status == 'completed':
            self.admission.useful({'owner': 'reviewer'})
            skipped = self.state.record_candidates(run_id, report['candidates'])
            for assessment in report['assessments']:
                cand = self.state.candidate(assessment['key'])
                if not cand or cand['disposition'] != 'accepted' or cand['assessed']:
                    continue
                self.state.record_assessment(assessment['key'], assessment)
                if assessment['outcome'] == 'rejected':
                    self.state.set('review:stage', 'report')
                    self.state.set('review:rollout_failure', assessment['observed'][:500])
                if assessment['outcome'] == 'confirmed' and self.review_stage(cfg) == 'pilot':
                    self.state.set('review:stage', 'full')
                    self.log('Review rollout: pilot improvement passed assessment; '
                             'daily operation enabled')
                if assessment['outcome'] == 'followup' and assessment.get('followup_candidate'):
                    self.state.record_candidates(run_id, [assessment['followup_candidate']])
        summary = json.dumps({'hashes': current.get('hashes'),
                              'candidates': [c['key'] for c in report['candidates']],
                              'suppressed': skipped,
                              'assessments': len(report['assessments'])})
        self.state.finish_review(run_id, status,
                                 completed_sha=current['base_sha'] if status != 'inconclusive' else None,
                                 report=summary)
        if status == 'inconclusive':
            if attempt == 1:
                self.state.set('review:retry_pending', slot)
            self.state.set('last_error', f'Review {run_id} reported inconclusive')
            self.log(f'Review {run_id} reported inconclusive')
            return
        self.state.set('review:cursor', self.state.get('review:cursor', 0) + 1)
        if (self.review_stage(cfg) == 'report' and cfg['auto_promote']
                and not self.state.get('review:rollout_failure')):
            self.state.set('review:stage', 'pilot')
            self.log('Review rollout: first completed report; pilot planner ingestion enabled')
        review_lane.prune_reports(self.root, time.time(), cfg['retention_days'],
                                  protected={run_id})
        self.log(f'Review {run_id} {status}: {summary}')

    def review(self, issues, prs):
        """Drive the daily architecture review lane."""
        cfg = review_lane.settings(self.config)
        current = self.state.get('reviewer')
        if current and not cfg['enabled']:
            self.runtime.terminate(current['process'])
            self.admission.release('reviewer')
            self.state.set('reviewer', None)
            self.finish_review_run(current['run_id'], current['slot'], current['attempt'],
                                   'inconclusive', 'Lane disabled during run')
            current = None
        if current:
            receipt = self.runtime.poll(current['process'])
            if receipt is None:
                return
            self.state.set('reviewer', None)
            log_path = Path(current['process'].get('log', '/nonexistent'))
            tail = log_path.read_text(errors='replace')[-12000:] if log_path.is_file() else ''
            category, retry_after = classify(receipt, tail)
            self.admission.finish('reviewer', current['process'], category, retry_after)
            self.ingest_review(current, receipt, cfg, issues)
        if self.state.get('reviewer'):
            return
        stale = self.state.running_review()
        if stale:
            self.state.finish_review(stale['run_id'], 'inconclusive',
                                     error='Review launch interrupted before invocation record')
        if not cfg['enabled'] or self.state.paused():
            return
        if self.slots_used() >= self.state.capacity():
            return
        slot = review_lane.current_slot(time.time(), cfg)
        if self.state.get('review:manual'):
            self.launch_review(cfg, slot, issues, prs, manual=True)
            return
        if self.state.get('review:last_slot') == slot:
            if self.state.get('review:retry_pending') == slot:
                self.state.set('review:retry_pending', None)
                self.launch_review(cfg, slot, issues, prs, attempt=2)
            return
        self.launch_review(cfg, slot, issues, prs)

    def refresh_improvements(self, issues):
        """Release the active-improvement slot when it can make no more progress.

        An improvement resolves once every mapped issue finished (done or
        blocked), closed, or can never be dispatched because a prerequisite
        ended blocked — a stalled dependency strand must not hold the slot
        forever.
        """
        active = self.state.get('review:active_improvement')
        if not active:
            return
        numbers = self.state.improvement_issues(active)
        statuses = {j['issue']: j['status'] for j in self.state.jobs()}
        by_number = {i['number']: i for i in issues}
        closed = {i['number'] for i in issues if i.get('state') == 'CLOSED'}

        def stalled(number, chain=()):
            """True when `number` can never dispatch: a prerequisite failed."""
            status = statuses.get(number)
            if status in ('working', 'pr-open') or number in closed:
                return False
            if status in ('done', 'blocked'):
                return status == 'blocked'
            issue = by_number.get(number)
            if issue is None or issue.get('state') != 'OPEN' or number in chain:
                return True
            try:
                deps = planning.metadata(issue.get('body') or '')['dependencies']
            except (ValueError, KeyError):
                return False
            return any(d not in closed and stalled(d, chain + (number,))
                       for d in deps)

        resolved = all(statuses.get(n) in ('done', 'blocked') or n in closed
                       or stalled(n) for n in numbers)
        if not numbers or resolved:
            self.state.set('review:active_improvement', None)
            self.log(f'Improvement {active} resolved; slot released')

    def apply_dispositions(self, dispositions, items, issues, created):
        """Apply planner accept/defer/reject decisions to review candidates."""
        for entry in dispositions:
            cand = self.state.candidate(entry['key'])
            if not cand or cand['disposition'] not in ('pending', 'accepted'):
                continue
            if entry['decision'] != 'accept':
                # Accepted work can still be deferred or rejected when it can
                # no longer proceed; its dispatched issues are not cancelled.
                self.state.disposition_candidate(entry['key'],
                    'deferred' if entry['decision'] == 'defer' else 'rejected',
                    entry['reason'], entry.get('revisit'),
                    sources=('pending', 'accepted'))
                self.log(f"Candidate {entry['key']} {entry['decision']}: {entry['reason'][:200]}")
                continue
            prefix = 'arch-' + entry['key']
            mapped = set()
            for item in items:
                number = created.get(item['key'])
                if number is not None and (item['key'] == prefix or item['key'].startswith(prefix + '-')
                                           or item.get('improvement') == entry['key']):
                    mapped.add(number)
            for issue in issues:
                try:
                    meta = planning.metadata(issue.get('body', ''))
                except (ValueError, KeyError):
                    continue
                if meta.get('improvement') == entry['key'] or meta['key'] == prefix or meta['key'].startswith(prefix + '-'):
                    mapped.add(issue['number'])
            if not mapped:
                self.state.disposition_candidate(entry['key'], 'deferred',
                    'Accepted without a mapped "arch-' + entry['key'] + '" issue in the proposal',
                    'Next planner pass that emits the mapped issue')
                continue
            self.state.disposition_candidate(entry['key'], 'accepted', entry['reason'],
                                             issue=min(mapped))
            for number in mapped:
                self.state.map_improvement(entry['key'], number)
            if cand['disposition'] == 'pending':
                self.log(f"Candidate {entry['key']} accepted; issues {sorted(mapped)}")

    def planner_review_input(self):
        """Undispositioned candidates, unresolved accepted work, and prior decisions."""
        cfg = review_lane.settings(self.config)
        qa_cfg = findings_lane.settings(self.config)
        review_open = cfg['enabled'] and self.review_stage(cfg) != 'report'
        pending, qa_pending = [], []
        for row in self.state.candidates('pending'):
            payload = json.loads(row['payload'])
            (qa_pending if payload.get('source') == 'qa' else pending).append(payload)
        # QA capability findings reach the planner even while the architecture
        # lane is staged at 'report'; architecture candidates stay gated.
        if not review_open:
            pending = []
        if not qa_cfg['enabled']:
            qa_pending = []
        accepted, suppressed = [], []
        if review_open:
            accepted = [{'key': c['key'], 'title': c['title'],
                         'issues': self.state.improvement_issues(c['key']),
                         'proposal': json.loads(c['payload'])}
                        for c in self.state.unresolved_accepted()]
            suppressed = [{'key': c['key'], 'disposition': c['disposition'],
                           'reason': c['reason'], 'revisit': c['revisit']}
                          for c in self.state.candidates()
                          if c['disposition'] in ('deferred', 'rejected')][:50]
        if not pending and not qa_pending and not accepted and not suppressed:
            return None
        return {'candidates': pending + qa_pending, 'accepted': accepted, 'suppressed': suppressed,
                'active_improvement': self.state.get('review:active_improvement')}

    def qa(self, issues):
        """Findings lane: ingest QA reports, route findings, publish dashboard.

        Deliberately fail-safe: a broken report or a down report channel must
        never wedge the production dispatch loop.
        """
        cfg = findings_lane.settings(self.config)
        if not cfg['enabled'] and not cfg['dashboard']:
            return
        try:
            findings_lane.poll(self.state, self.github, cfg, issues, self.log)
        except Exception as exc:
            self.log('QA findings lane failed: ' + str(exc)[:500])

    def ready_frontier(self, issues):
        rows = scheduling.inventory(issues, self.state.jobs(),
                                    self.state.get('review:active_improvement'))
        return sum(row['reason'] == 'ready' for row in rows)

    def planner(self, issues, prs):
        current = self.state.get('planner')
        if current:
            receipt = self.runtime.poll(current['process'])
            if receipt is None:
                return
            self.state.set('planner', None)
            log_path = Path(current['process'].get('log', '/nonexistent'))
            tail = log_path.read_text(errors='replace')[-12000:] if log_path.is_file() else ''
            category, retry_after = classify(receipt, tail)
            self.admission.finish('planner', current['process'], category, retry_after)
            if receipt.get('returncode', receipt.get('exit_code', -1)) != 0:
                self.log('Planner failed: ' + json.dumps(receipt))
                return
            if not Path(current['output']).is_file():
                self.state.set('last_error', 'Planner produced no proposal; inspect its permission/output log')
                self.log('Planner produced no proposal; inspect its permission/output log')
                return
            try:
                proposal = planning.validate(json.loads(Path(current['output']).read_text()))
            except ValueError as exc:
                self.state.set('planner_feedback', str(exc))
                self.state.set('last_error', 'Planner proposal rejected: ' + str(exc)[:500])
                self.log('Planner proposal rejected: ' + str(exc))
                return
            self.state.set('planner_feedback', None)
            self.admission.useful({'owner': 'planner'})
            self.state.set('pending_proposal', proposal)
        if self.state.paused():
            return
        pending = self.state.get('pending_proposal')
        if isinstance(pending, list):
            # Supervisors before the review lane persisted the bare issues list.
            pending = {'issues': pending, 'dispositions': []}
        if pending is not None:
            if not isinstance(pending, dict) or not isinstance(pending.get('issues'), list):
                self.state.set('pending_proposal', None)
                self.state.set('last_error', 'Discarded malformed pending proposal')
                self.log('Discarded malformed pending proposal')
                return
            try:
                pending = planning.validate(pending)
            except ValueError as exc:
                self.state.set('pending_proposal', None)
                self.state.set('planner_feedback', str(exc))
                self.state.set('last_error', 'Discarded malformed pending proposal: ' + str(exc)[:500])
                self.log('Discarded malformed pending proposal: ' + str(exc))
                return
            self.state.set('planner_feedback', None)
            # The frontier cap must count dispatchable work, not labels on
            # tickets whose prerequisites are still open.
            ready_count = self.ready_frontier(issues)
            closed = {i['number'] for i in issues if i.get('state') == 'CLOSED'}
            known = {}
            for issue in issues:
                try:
                    known[planning.metadata(issue.get('body', ''))['key']] = issue['number']
                except (ValueError, KeyError):
                    pass
            numbers = {i['number'] for i in issues}
            created = {}
            for item in planning.ordered_issues(pending['issues']):
                if item['key'] in known:
                    continue
                if ready_count >= 20:
                    continue
                resolved = [known[d] if isinstance(d, str) else d for d in item['dependencies']]
                if not set(resolved) <= numbers:
                    raise ValueError('Planner referenced nonexistent dependencies')
                published = dict(item, dependencies=resolved)
                number = self.github.create_issue(item['title'], planning.body(published),
                    ['agent:ready', f"priority:P{item['priority']}", areas.label(item['area'])], item['key'])
                created[item['key']] = number
                known[item['key']] = number
                numbers.add(number)
                if item.get('improvement'):
                    self.state.map_improvement(item['improvement'], number)
                if set(resolved) <= closed:
                    ready_count += 1
            self.apply_dispositions(pending.get('dispositions', []), pending['issues'], issues, created)
            self.state.set('pending_proposal', None)
            return
        now = time.time()
        last = self.state.get('last_plan', 0)
        frontier = self.ready_frontier(issues)
        armed_retries = sum(bool(self.state.get('retry:' + str(job['issue'])))
                            for job in self.state.jobs(('blocked',)))
        forced = bool(self.state.get('plan:requested'))
        if not scheduling.due_for_planning(now, last, frontier, armed_retries,
                                           forced=forced):
            return
        if not self.admission.reserve('planner', self.models[0], 'coordinator', self.state.capacity()):
            self.log('Planner deferred: inference admission denied')
            return
        try:
            clone = self.runtime.prepare_clone('coordinator')
            output = clone / '.dcs-agent' / f'proposal-{int(now)}.json'
            output.parent.mkdir(parents=True, exist_ok=True)
            allocation = areas.Allocation.from_inventory(issues, self.state.jobs())
            process = self.runtime.spawn('planner-' + str(int(now)), clone, planning.prompt(
                issues, prs, output, self.planner_review_input(), self.state.get('planner_feedback'), allocation.summary(), self.state.merge_flow()), model=self.models[0])
        except Exception:
            self.admission.release('planner')
            raise
        self.admission.attach('planner', process)
        self.state.set('planner', {'process': process, 'output': str(output)})
        self.state.set('last_plan', now)
        self.state.set('plan:requested', False)
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
        # Priority labels must agree with managed metadata; dispatch sorts by metadata.
        for issue in issues:
            if issue.get('state') != 'OPEN':
                continue
            try:
                meta = planning.metadata(issue.get('body') or '')
            except (ValueError, KeyError):
                continue
            labels = [l['name'] for l in issue.get('labels', [])]
            want = f"priority:P{meta.get('priority', 3)}"
            # Older managed issues may have their area only as a GitHub
            # label. Preserve and honor that classification until their
            # metadata is migrated; future proposals store both.
            area = meta.get('area') or areas.issue_area(issue)
            wanted_area = areas.label(area) if area else None
            wrong = [l for l in labels if l.startswith('priority:P') and l != want]
            wrong_areas = [l for l in labels if wanted_area and
                           l.startswith('area:') and l != wanted_area]
            add = []
            if want not in labels and 'agent:ready' in labels:
                add.append(want)
            if wanted_area and wanted_area not in labels:
                add.append(wanted_area)
            if wrong or wrong_areas or add:
                self.github.update_issue(issue['number'],
                                         add_labels=add,
                                         remove_labels=wrong + wrong_areas)
        self.state.set('area_allocation', areas.Allocation.from_inventory(
            issues, self.state.jobs()).summary())

    def retries(self, issues):
        if self.state.paused():
            return
        by_number = {i['number']: i for i in issues}
        for job in self.state.jobs(('blocked',)):
            if not self.state.get('retry:' + str(job['issue'])) or job['issue'] not in by_number:
                continue
            active = self.state.jobs(('working', 'pr-open'))
            if self.slots_used(active) >= self.state.capacity():
                continue
            rec = self.state.get('recovery:' + str(job['issue']))
            if rec is None:
                rec = self.capture_recovery(job)
            self.recover_job(job, rec, active, by_number[job['issue']])

    def dispatch(self, issues):
        if self.state.paused():
            return
        self.refresh_improvements(issues)
        capacity = self.state.capacity()
        if self.slots_used() >= capacity:
            return
        allocation = areas.Allocation.from_inventory(issues, self.state.jobs())
        self.state.set('area_allocation', allocation.summary())
        closed = {i['number'] for i in issues if i.get('state') == 'CLOSED'}
        def rank(issue):
            try:
                meta = planning.metadata(issue.get('body', ''))
                area = meta.get('area') or areas.issue_area(issue)
                return (meta.get('priority', 3),
                        allocation.score(area) if area else float('inf'),
                        issue['number'])
            except (ValueError, KeyError):
                return 4, float('inf'), issue['number']
        candidates = list(issues)
        while candidates:
            issue = min(candidates, key=rank)
            candidates.remove(issue)
            active_improvement = self.state.get('review:active_improvement')
            reason, meta = scheduling.classify(
                issue, {job['issue']: job for job in self.state.jobs()},
                closed, active_improvement)
            if reason != 'ready':
                continue
            area = meta.get('area') or areas.issue_area(issue)
            improvement = meta.get('improvement')
            # The reviewer slot is re-enforced after every reservation; worker
            # clone leasing alone would fill every worker slot past the ceiling.
            if self.slots_used() >= capacity:
                return
            free = self.free_workers(self.state.jobs(('working', 'pr-open')))
            if not free:
                return
            worker = self.admit_worker(issue['number'], free)
            if not worker:
                return
            job = self.state.reserve(issue['number'], worker, meta['group'])
            if not job:
                self.admission.release('job:' + str(issue['number']))
                continue
            if improvement and not active_improvement:
                self.state.set('review:active_improvement', improvement)
            allocation.note(area)
            self.state.set('area_allocation', allocation.summary())
            try:
                self.launch(job, issue)
            except Exception as exc:
                self.block(job, str(exc))

    def run(self):
        # Lazy: the supervisor is POSIX-only, but the module must stay
        # importable on Windows so the repository test suite can collect it.
        import fcntl
        with (self.root / 'supervisor.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'stopping', True))
            signal.signal(signal.SIGINT, lambda *_: setattr(self, 'stopping', True))
            self.github.ensure_labels()
            self.recover_processes()
            owners = {'job:' + str(j['issue']) for j in self.state.jobs(('working',))}
            if self.state.get('planner'):
                owners.add('planner')
            if self.state.get('reviewer'):
                owners.add('reviewer')
            self.admission.reconcile(owners)
            delay = self.config['poll_seconds']
            self.log('Supervisor started; models: ' + ', '.join(self.models))
            try:
                while not self.stopping:
                    try:
                        issues = self.github.issues(state='all')
                        prs = self.github.prs()
                        self.reconcile_workers(issues)
                        self.integrate(issues)
                        self.review(issues, prs)
                        self.planner(issues, prs)
                        self.retries(issues)
                        self.dispatch(issues)
                        self.mirror(issues)
                        self.qa(issues)
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
                        reviewer = self.state.get('reviewer')
                        if reviewer:
                            records.append(reviewer['process'])
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
                reviewer = self.state.get('reviewer')
                if reviewer:
                    self.runtime.terminate(reviewer['process'])
                self.log('Supervisor stopped; work preserved')
