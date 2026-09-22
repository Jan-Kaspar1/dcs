"""Small GitHub CLI adapter; merge decisions fail closed."""
import json
import subprocess
import tempfile

from . import areas


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo, cwd=None):
        self.repo, self.cwd = repo, cwd

    def run(self, *args, json_output=False):
        result = subprocess.run(['gh', *args], cwd=self.cwd, capture_output=True, text=True)
        if result.returncode:
            raise GitHubError(result.stderr.strip() or result.stdout.strip())
        if json_output:
            try:
                return json.loads(result.stdout)
            except ValueError as exc:
                raise GitHubError('Invalid GitHub JSON response') from exc
        return result.stdout.strip()

    def api(self, endpoint):
        return self.run('api', endpoint, json_output=True)

    def issues(self, state='open'):
        return self.run('issue', 'list', '--repo', self.repo, '--state', state, '--limit', '1000',
                        '--json', 'number,title,body,labels,state,url', json_output=True)

    def issue(self, number):
        return self.run('issue', 'view', str(number), '--repo', self.repo,
                        '--json', 'number,title,body,labels,state,url', json_output=True)

    def ensure_labels(self):
        labels = ['agent:' + s for s in ('ready','working','pr-open','blocked')] + [f'worker:worker-{n:02}' for n in range(1,21)] + [f'priority:P{n}' for n in range(4)]
        for label in labels:
            self.run('label', 'create', label, '--repo', self.repo, '--force')
        colors = ('1d76db', '5319e7', 'b60205', '0052cc', '0e8a16',
                  'd93f0b', 'fbca04', '006b75', 'c2e0c6', '6f42c1')
        for (area, (_weight, description)), color in zip(areas.DEFINITIONS.items(), colors):
            self.run('label', 'create', areas.label(area), '--repo', self.repo,
                     '--color', color, '--description', description, '--force')

    def create_issue(self, title, body, labels=(), key=None):
        marker = '<!-- dcs-agent-key:' + key + ' -->' if key else None
        if marker:
            for issue in self.issues('all'):
                if marker in (issue.get('body') or ''):
                    return issue['number']
            body += '\n\n' + marker
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as file:
            file.write(body)
            file.flush()
            args = ['issue','create','--repo',self.repo,'--title',title,'--body-file',file.name]
            for label in labels:
                args += ['--label',label]
            url = self.run(*args)
        return int(url.rstrip('/').split('/')[-1])

    def update_issue(self, number, title=None, body=None, add_labels=(), remove_labels=()):
        args = ['issue','edit',str(number),'--repo',self.repo]
        if title is not None:
            args += ['--title',title]
        for label in add_labels:
            args += ['--add-label',label]
        for label in remove_labels:
            args += ['--remove-label',label]
        if body is None:
            return self.run(*args)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as file:
            file.write(body)
            file.flush()
            return self.run(*args,'--body-file',file.name)

    def comment(self, number, body):
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as file:
            file.write(body)
            file.flush()
            return self.run('issue','comment',str(number),'--repo',self.repo,'--body-file',file.name)

    def prs(self):
        return self.run('pr','list','--repo',self.repo,'--state','open','--limit','1000',
                        '--json','number,headRefName,headRefOid,baseRefName,state,url',json_output=True)

    def find_pr(self, branch):
        rows = self.run('pr','list','--repo',self.repo,'--head',branch,'--state','all','--limit','100',
                        '--json','number,headRefName,headRefOid,baseRefName,state,url',json_output=True)
        rows = [r for r in rows if r['headRefName'] == branch]
        if len(rows) > 1:
            raise GitHubError('Multiple PRs for branch ' + branch)
        return rows[0] if rows else None

    def create_pr(self, branch, title, body):
        existing = self.find_pr(branch)
        if existing:
            return existing['number']
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as file:
            file.write(body)
            file.flush()
            url = self.run('pr','create','--repo',self.repo,'--base','main','--head',branch,
                           '--title',title,'--body-file',file.name)
        return int(url.rstrip('/').split('/')[-1])

    def pr(self, number):
        return self.api(f'repos/{self.repo}/pulls/{number}')

    def main_sha(self):
        return self.api(f'repos/{self.repo}/git/ref/heads/main')['object']['sha']

    def includes_main(self, head, base):
        comparison = self.api(f'repos/{self.repo}/compare/{base}...{head}')
        return comparison.get('status') in ('ahead','identical') and comparison.get('merge_base_commit',{}).get('sha') == base

    def check_states(self, sha):
        raw = self.run('api','--paginate',f'repos/{self.repo}/commits/{sha}/check-runs?per_page=100')
        pages = []
        decoder = json.JSONDecoder()
        while raw.strip():
            page, consumed = decoder.raw_decode(raw.lstrip())
            pages.append(page)
            raw = raw.lstrip()[consumed:]
        states = {}
        # Only newest run of a repeated check name is authoritative.
        for check in sorted((c for p in pages for c in p.get('check_runs',[])), key=lambda c:c['id']):
            if check.get('head_sha') != sha:
                continue
            states[check['name']] = check.get('conclusion') if check.get('status') == 'completed' else 'pending'
        return states

    def checks_pass(self, pr, required):
        sha = pr['head']['sha'] if isinstance(pr, dict) else pr
        states = self.check_states(sha)
        return bool(required) and all(states.get(name) == 'success' for name in required)

    def merge(self, number, required):
        first = self.pr(number)
        if first.get('merged'):
            return True
        if first.get('state') != 'open' or first.get('draft') or first['base']['ref'] != 'main':
            return False
        head, base = first['head']['sha'], self.main_sha()
        if not self.includes_main(head, base) or not self.checks_pass(first, required):
            return False
        current = self.pr(number)
        if current['head']['sha'] != head or self.main_sha() != base or current.get('state') != 'open':
            return False
        # GitHub checks the expected head atomically. Base is checked immediately above;
        # unrelated external pushes cannot be atomically fenced without branch protection.
        self.run('pr','merge',str(number),'--repo',self.repo,'--squash','--match-head-commit',head)
        return bool(self.pr(number).get('merged'))
