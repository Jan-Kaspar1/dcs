"""Host firewall policy for the QA lane's Docker bridges.

A Docker bridge alone is not a network restriction: disabled
masquerade only removes NAT — it does not stop a container from
emitting packets toward the LAN, other container subnets, or host
sockets reachable through the bridge gateway. This module renders,
applies, and verifies an explicit host policy in the reserved
DOCKER-USER chain (the operator hook Docker evaluates first in
FORWARD and never rewrites) plus INPUT rules for host-socket access,
mirrored into ip6tables so a future IPv6-enabled build cannot route
around it.

Two bridge interfaces are managed, named deterministically through
com.docker.network.bridge.name (the cycle flock guarantees a single
active run, so fixed names cannot collide):

  dcsqab   builder network. `cargo build --locked` fetches from the
           crates.io sparse index (index.crates.io) and crate files
           (static.crates.io), both served from Fastly CDN edges whose
           addresses rotate too quickly for an address allowlist. The
           enforced bound is therefore: new outbound TCP 80/443 to
           non-private destinations only. DNS resolves through
           Docker's embedded 127.0.0.11 resolver, which never enters
           the forwarding path, so no DNS rule is needed or granted.
  dcsqar   rig network. No egress: only traffic belonging to inbound
           loopback-published monitor connections passes.

All QA interfaces share the `dcsqa` prefix. In FORWARD a jump sends
every packet sourced from a QA bridge through the DCSQA-EGRESS chain,
ending in a catch-all drop for any QA interface the explicit rules
did not cover. In INPUT, established replies to host-originated
monitor connections are accepted, then every other packet from a QA
bridge addressed to the host itself is dropped — that denies access
to host services and to other stacks' published ports via the host
address, while leaving monitor replies working. That INPUT drop is
the rig bridge-to-host reachability rule the qax-20260922-001,
qax-20260922-005, and qax-20260923-001 runs demonstrated: no socket
bound on the host is reachable from a rig bridge, so a lane endpoint
a rig container must dial runs bridge-placed in a labeled
rig-network container — the selection the run config records under
endpoint_placement (qa_lane.runner).

apply() is idempotent (the policy chain is rebuilt and hook rules
inserted once); verify() returns the list of missing required rules
so the cycle can fail closed when the policy is absent. Both run
through `sudo -n` unless the process is already root.
"""
import os
import subprocess

CHAIN = 'DCSQA-EGRESS'
IFACE_PREFIX = 'dcsqa'
BUILDER_IFACE = 'dcsqab'
RIG_IFACE = 'dcsqar'

# Destinations the builder must never reach: host LAN, link-local,
# CGNAT, loopback, multicast/reserved, benchmarking space, and the
# RFC 1918 blocks that cover every other Docker bridge on this host.
BUILDER_DENY_DESTS = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10',
                      '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
                      '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4',
                      '240.0.0.0/4')
BUILDER_ALLOW_PORTS = ('80', '443')


def _v4_chain_rules():
    """Rules inside the v4 policy chain, in order."""
    rules = [
        ['-i', BUILDER_IFACE, '-m', 'conntrack', '--ctstate',
         'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
    ]
    for dest in BUILDER_DENY_DESTS:
        rules.append(['-i', BUILDER_IFACE, '-d', dest, '-j', 'DROP'])
    rules += [
        ['-i', BUILDER_IFACE, '-p', 'tcp', '-m', 'multiport',
         '--dports', ','.join(BUILDER_ALLOW_PORTS), '-j', 'ACCEPT'],
        ['-i', BUILDER_IFACE, '-j', 'DROP'],
        ['-i', RIG_IFACE, '-m', 'conntrack', '--ctstate',
         'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
        ['-i', RIG_IFACE, '-j', 'DROP'],
        ['-i', IFACE_PREFIX + '+', '-j', 'DROP'],
        ['-j', 'RETURN'],
    ]
    return rules


def _v6_chain_rules():
    # No QA bridge carries IPv6; drop anything that ever appears.
    return [['-i', IFACE_PREFIX + '+', '-j', 'DROP'], ['-j', 'RETURN']]


def _hook_rules():
    """Hook rules outside the policy chain: DOCKER-USER jump + INPUT."""
    return [
        ('DOCKER-USER',
         ['-i', IFACE_PREFIX + '+', '-j', CHAIN]),
        ('INPUT',
         ['-i', IFACE_PREFIX + '+', '-m', 'conntrack', '--ctstate',
          'ESTABLISHED,RELATED', '-j', 'ACCEPT']),
        ('INPUT', ['-i', IFACE_PREFIX + '+', '-j', 'DROP']),
    ]


def required_checks():
    """Every rule that must exist for the policy to be enforced.

    Each item is (tool, chain, rule-args); the chain-existence probe
    carries the pseudo-rule ['-L', CHAIN, '-n'] instead of a -C check.
    """
    checks = []
    for tool, rules in (('iptables', _v4_chain_rules()),
                        ('ip6tables', _v6_chain_rules())):
        checks.append((tool, CHAIN, ['-L', CHAIN, '-n']))
        for rule in rules:
            checks.append((tool, CHAIN, rule))
        for hook, rule in _hook_rules():
            checks.append((tool, hook, rule))
    return checks


def _base_cmd(tool, runner):
    if runner is not None:
        return runner([tool])
    cmd = [tool]
    if os.geteuid() != 0:
        cmd = ['sudo', '-n'] + cmd
    return cmd


def _run(tool, args, runner):
    return subprocess.run(_base_cmd(tool, runner) + args,
                          capture_output=True, text=True, timeout=30)


def verify(runner=None):
    """Return the list of missing required rules ([] = enforced).

    `runner`, when given, maps a command list to a full command list —
    used by tests and by callers with a different privilege path.
    """
    missing = []
    for tool, chain, rule in required_checks():
        if rule[:2] == ['-L', CHAIN]:
            result = _run(tool, rule, runner)
        else:
            result = _run(tool, ['-C', chain] + rule, runner)
        if result.returncode != 0:
            missing.append(tool + ' ' + chain + ' ' + ' '.join(rule))
    return missing


def apply(runner=None):
    """Idempotently install the policy; returns fired command count."""
    fired = 0
    for tool, rules in (('iptables', _v4_chain_rules()),
                        ('ip6tables', _v6_chain_rules())):
        if _run(tool, ['-L', CHAIN, '-n'], runner).returncode != 0:
            _run(tool, ['-N', CHAIN], runner)
        else:
            _run(tool, ['-F', CHAIN], runner)
        fired += 1
        for rule in rules:
            _run(tool, ['-A', CHAIN] + rule, runner)
            fired += 1
        for hook, rule in _hook_rules():
            if _run(tool, ['-C', hook] + rule, runner).returncode != 0:
                _run(tool, ['-A', hook] + rule, runner)
                fired += 1
    return fired


def main(argv=None):
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ['apply']:
        print('netpolicy: applied (' + str(apply()) + ' commands)')
        missing = verify()
        if missing:
            print('netpolicy: still missing:')
            for item in missing:
                print('  ' + item)
            return 1
        print('netpolicy: verified')
        return 0
    if args == ['verify']:
        missing = verify()
        for item in missing:
            print(item)
        return 1 if missing else 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
