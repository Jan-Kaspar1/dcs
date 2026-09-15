#!/usr/bin/env python3
"""Install a reviewed committed revision without starting it."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

def main():
    source = Path(__file__).resolve().parents[1]
    active = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'dcs-agents.service']).returncode == 0
    if active:
        raise SystemExit('Stop dcs-agents before upgrading its pinned release')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=source, text=True).strip():
        raise SystemExit('Commit the reviewed source before installing a pinned release')
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=source, check=True)
    home = Path.home()
    base = home / '.local/share/dcs-agents'
    release = base / 'releases' / revision
    release.mkdir(parents=True, exist_ok=True)
    for directory in ('agent_pool', 'qa_lane', 'scripts'):
        shutil.copytree(source / directory, release / directory, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    (release / 'REVISION').write_text(revision + '\n')
    current = base / 'current'
    temp = base / 'current.new'
    temp.unlink(missing_ok=True)
    temp.symlink_to(release)
    temp.replace(current)
    bindir = home / '.local/bin'
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / 'dcs-agents'
    launcher.write_text('#!/bin/sh\nexport PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"\nexport PYTHONPATH="$HOME/.local/share/dcs-agents/current"\nexec /usr/bin/python3 -m agent_pool "$@"\n')
    launcher.chmod(0o755)
    configdir = home / '.config/dcs-agents'
    configdir.mkdir(parents=True, exist_ok=True)
    configfile = configdir / 'config.json'
    if not configfile.exists():
        configfile.write_text(json.dumps({'repository':'Jan-Kaspar1/dcs','model':'swe-2-high','pool_root':str(home / 'workspace/dcs-agent-pool'),'state_root':str(base / 'state'),'required_checks':['rust-format','rust-clippy','rust-tests','supervisor-tests'],'timeout_seconds':7200,'poll_seconds':60,'review':{'enabled':False,'mode':'report','auto_promote':True,'time':'03:00','timezone':'Europe/Berlin','timeout_seconds':3600,'max_candidates':3,'retention_days':30}}, indent=2) + '\n')
        configfile.chmod(0o600)
    units = home / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    (units / 'dcs-agents.service').write_text('[Unit]\nDescription=DCS local Devin worker supervisor\nAfter=network-online.target\n\n[Service]\nType=simple\nExecStart=%h/.local/bin/dcs-agents run\nRestart=on-failure\nRestartSec=30\nTimeoutStopSec=45\nKillMode=control-group\nEnvironment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n\n[Install]\nWantedBy=default.target\n')
    subprocess.run(['systemctl','--user','daemon-reload'], check=True)
    print('Installed revision ' + revision + '. Service remains stopped until explicitly started.')

if __name__ == '__main__':
    main()
