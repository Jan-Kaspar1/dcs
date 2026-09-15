"""QA lane CLI — runs on the Lenovo host (and for `validate`/`status`
anywhere a config or report file is reachable).

  python3 -m qa_lane status              state summary as JSON
  python3 -m qa_lane enqueue <sha>       queue a revision for testing
  python3 -m qa_lane cycle               reconcile, then run newest queued
  python3 -m qa_lane reconcile           reap dead runs and orphans only
  python3 -m qa_lane reclaim             retention pass only (no run)
  python3 -m qa_lane preserve <spec> [on|off]
                                         pin run:<id> or sha:<sha>
                                         evidence against retention;
                                         no spec lists current pins
  python3 -m qa_lane netpolicy <apply|verify>
                                         host egress firewall policy
                                         (needs root; the dedicated
                                         netpolicy unit applies it)
  python3 -m qa_lane validate <file>     validate a report document
"""
import json
import sys
import time
from pathlib import Path

from . import netpolicy, report as qa_report
from . import runner, state as qa_state

GIT_SHA_LEN = 40


def _preserve(st, rest):
    if not rest:
        print(json.dumps(st.preserved(), indent=1))
        return
    spec = rest[0]
    on = not (len(rest) > 1 and rest[1] == 'off')
    if ':' not in spec:
        raise SystemExit('preserve expects run:<id> or sha:<sha> '
                         '[on|off]')
    kind, value = spec.split(':', 1)
    pins = st.set_preserve(kind, value, on)
    print(json.dumps(pins))


def _enqueue(cfg, sha):
    if len(sha) != GIT_SHA_LEN or any(c not in '0123456789abcdef'
                                      for c in sha):
        raise SystemExit('enqueue expects a 40-hex revision')
    st = qa_state.State(Path(cfg['state_dir']) / 'state.db')
    try:
        now = time.time()
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        record = st.enqueue(runner._new_run_id(st, now), sha, now, day)
        print(json.dumps({'queued': record['run_id'],
                          'sha': record['attempted_sha'],
                          'attempt': record['attempt'],
                          'range_first': record['range_first']}))
    finally:
        st.close()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    command, rest = args[0], args[1:]
    if command == 'validate':
        if not rest:
            raise SystemExit('validate needs a report path')
        qa_report.validate_report(Path(rest[0]).read_text())
        print(rest[0] + ': valid schema v' + str(qa_report.SCHEMA_VERSION))
        return 0
    cfg = runner.load_config()
    if command == 'status':
        print(json.dumps(runner.status(cfg), indent=1))
    elif command == 'enqueue':
        if not rest:
            raise SystemExit('enqueue needs a revision')
        _enqueue(cfg, rest[0])
    elif command == 'reconcile':
        st = qa_state.State(Path(cfg['state_dir']) / 'state.db')
        try:
            runner.reconcile(st, cfg)
        finally:
            st.close()
    elif command == 'reclaim':
        st = qa_state.State(Path(cfg['state_dir']) / 'state.db')
        try:
            runner.reclaim(st, cfg)
        finally:
            st.close()
    elif command == 'preserve':
        st = qa_state.State(Path(cfg['state_dir']) / 'state.db')
        try:
            _preserve(st, rest)
        finally:
            st.close()
    elif command == 'netpolicy':
        if len(rest) != 1 or rest[0] not in ('apply', 'verify'):
            raise SystemExit('netpolicy expects apply or verify')
        return netpolicy.main(rest)
    elif command == 'cycle':
        runner.cycle(cfg)
    else:
        raise SystemExit('unknown command: ' + command)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
