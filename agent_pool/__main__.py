"""Operator CLI for the pinned local installation."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from .config import load
from .state import State


def qa_section(config, state):
    """status's qa section: persisted counters plus the configured lane state
    (the dashboard reads routing on/off from here)."""
    qa = state.qa_summary()
    try:
        from . import findings
        cfg = findings.settings(config)
        qa.update(enabled=cfg['enabled'], mode=cfg['mode'],
                  dashboard=cfg['dashboard'])
    except Exception:
        pass
    return qa

def main():
    parser = argparse.ArgumentParser(prog='dcs-agents')
    parser.add_argument('--config')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('start', 'pause', 'resume', 'stop', 'status', 'logs', 'run', 'wait-service'):
        commands.add_parser(name)
    commands.add_parser('plan')
    timeline_cmd = commands.add_parser('timeline')
    timeline_cmd.add_argument('issue', nargs='?', type=int)
    explain_cmd = commands.add_parser('explain')
    explain_cmd.add_argument('--issue', type=int)
    admission_cmd = commands.add_parser('admission')
    admission_cmd.add_argument('action', nargs='?', choices=('status', 'reset'), default='status')
    admission_cmd.add_argument('group', nargs='?')
    retry = commands.add_parser('retry')
    retry.add_argument('issue', type=int)
    review_cmd = commands.add_parser('review')
    review_cmd.add_argument('action', nargs='?', choices=('run',))
    upgrade = commands.add_parser('upgrade')
    upgrade.add_argument('source', help='Reviewed source checkout to install')
    args = parser.parse_args()
    config = load(args.config)
    state = State(Path(config['state_root']) / 'state.sqlite3')
    if args.command == 'run':
        from .supervisor import Supervisor
        Supervisor(config).run()
    elif args.command == 'pause':
        state.pause()
    elif args.command == 'resume':
        state.resume()
    elif args.command == 'start':
        subprocess.run(['systemctl', '--user', 'start', 'dcs-agents.service'], check=True)
    elif args.command == 'stop':
        subprocess.run(['systemctl', '--user', 'stop', 'dcs-agents.service'], check=True)
    elif args.command == 'status':
        from .admission import Admission
        print(json.dumps({'paused':state.paused(), 'pause_reason':state.get('pause_reason'), 'integrity_error':state.get('integrity_error'), 'last_error':state.get('last_error'), 'capacity':state.capacity(), 'merges':state.get('merges',0), 'planner':state.get('planner'), 'areas':state.get('area_allocation') or {}, 'admission':Admission(state, config).summary(), 'review':state.review_summary(), 'qa':qa_section(config, state), 'jobs':state.jobs()}, indent=2))
    elif args.command == 'plan':
        if state.paused():
            parser.error('Resume before requesting a planning pass')
        state.set('plan:requested', True)
        print('Planning pass requested; the supervisor will start it when admission permits.')
    elif args.command == 'timeline':
        print(json.dumps(state.events(args.issue), indent=2))
    elif args.command == 'explain':
        from .admission import Admission
        from .github import GitHub
        from .scheduling import explain
        issues = GitHub(config['repository']).issues(state='all')
        jobs = state.jobs()
        retries = [job['issue'] for job in jobs if job['status'] == 'blocked'
                   and state.get('retry:' + str(job['issue']))]
        admission = Admission(state, config)
        view = explain(issues, jobs, admission.summary(), retries,
                       state.get('review:active_improvement'),
                       configured_groups=set(admission.groups))
        if args.issue is None:
            view.pop('issues')
        else:
            view['issues'] = [row for row in view['issues'] if row['issue'] == args.issue]
        print(json.dumps(view, indent=2))
    elif args.command == 'admission':
        from .admission import Admission
        admission = Admission(state, config)
        if args.action == 'reset':
            if not args.group:
                parser.error('admission reset requires a quota group name')
            try:
                admission.reset(args.group)
            except ValueError as exc:
                parser.error(str(exc) + '; known groups: ' + ', '.join(sorted(admission.groups)))
            print('Quota group ' + args.group + ' reset to normal admission.')
        else:
            print(json.dumps(admission.summary(), indent=2))
    elif args.command == 'review':
        if args.action == 'run':
            if state.paused():
                parser.error('Resume before requesting a manual review')
            state.set('review:manual', True)
            print('Manual architecture review queued; the supervisor runs it at the next cycle.')
        else:
            print(json.dumps(state.review_summary(), indent=2, default=str))
    elif args.command == 'logs':
        subprocess.run(['tail', '-n', '100', str(Path(config['state_root']) / 'supervisor.log')], check=True)
    elif args.command == 'retry':
        if state.paused():
            parser.error('Resume before requesting retry')
        if not state.job(args.issue) or state.job(args.issue)['status'] != 'blocked':
            parser.error('Only blocked jobs can be retried')
        state.set('retry:' + str(args.issue), True)
        print('Retry queued; supervisor will reconcile and preserve previous work.')
    elif args.command == 'upgrade':
        subprocess.run([sys.executable, str(Path(args.source).resolve() / 'scripts/install_agents.py')], check=True)
    elif args.command == 'wait-service':
        # Keepalive: start the supervisor once, then hold WSL open. Exiting
        # when the service stops would strand upgrades, which require the stop.
        subprocess.run(['systemctl', '--user', 'start', 'dcs-agents.service'], check=True)
        while True:
            time.sleep(3600)

if __name__ == '__main__':
    main()
