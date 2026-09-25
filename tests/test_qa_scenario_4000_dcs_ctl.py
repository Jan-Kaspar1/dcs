"""The 4000_dcs_ctl leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_dcs_ctl, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DcsCtlTests.test_registered_last_and_replayable',
    'DcsCtlTests.test_clean_rig_passes_with_full_evidence',
    'DcsCtlTests.test_two_runs_produce_identical_evidence',
    'DcsCtlTests.test_role_mismatch_fails',
    'DcsCtlTests.test_missing_schema_kind_fails',
    'DcsCtlTests.test_resources_missing_component_fails',
    'DcsCtlTests.test_resources_entry_mismatch_fails',
    'DcsCtlTests.test_keyed_events_missing_component_fails',
    'DcsCtlTests.test_receipts_missing_invoke_fails',
    'DcsCtlTests.test_history_without_samples_fails',
    'DcsCtlTests.test_no_bound_measurement_is_inconclusive',
    'DcsCtlTests.test_missing_receipt_fails',
    'DcsCtlTests.test_unjournaled_settlement_fails',
    'DcsCtlTests.test_unattributed_actor_fails',
    'DcsCtlTests.test_stale_identical_settlement_is_not_this_legs',
    'DcsCtlTests.test_carried_over_settlement_is_not_this_legs',
    'DcsCtlTests.test_unattributed_event_fails',
    'DcsCtlTests.test_silently_accepted_undeclared_invoke_fails',
    'DcsCtlTests.test_unavailable_binary_is_inconclusive',
    'DcsCtlTests.test_mutation_legs_cover_each_write_verb',
    'DcsCtlTests.test_mutation_leg_payloads_show_attribution',
    'DcsCtlTests.test_mutation_rejection_legs_name_their_refusals',
    'DcsCtlTests.test_paced_scan_leg_records_a_skipped_attempt',
    'DcsCtlTests.test_driven_scan_leg_verifies_served_tick',
    'DcsCtlTests.test_unserved_write_fails',
    'DcsCtlTests.test_unserved_tune_fails',
    'DcsCtlTests.test_unbadged_force_fails',
    'DcsCtlTests.test_stuck_unforce_fails',
    'DcsCtlTests.test_standby_accepting_commands_fails',
    'DcsCtlTests.test_demote_refusal_fails_and_restores',
    'DcsCtlTests.test_promote_refusal_fails',
    'DcsCtlTests.test_unsettled_switch_fails',
    'DcsCtlTests.test_missing_role_events_fail',
    'DcsCtlTests.test_unsettled_write_leg_fails',
    'DcsCtlTests.test_foreign_leg_actor_fails',
    'DcsCtlTests.test_no_writable_bool_is_inconclusive',
    'DcsCtlTests.test_no_tunable_parameter_is_inconclusive',
    'DcsCtlTests.test_snapshot_failure_is_inconclusive',
})


class CtlResult:
    """A faked CompletedProcess for the _run_ctl seam."""

    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CtlFeed:
    """The faked-subprocess side of the dcs-ctl scenario: run_ctl
    answers each argv the way the real binary would against the
    post-failover rig — reads print the served payloads and exit 0,
    receipted subcommands print the receipt and exit nonzero on a
    named rejection, and the switch/scan verbs answer transitional
    RoleReports or named refusals — while http_json covers the raw
    liveness gate. Mutable per-peer roles and journals, point values,
    standing forces, and live parameters give the mutation legs their
    served-state halves. Fault flags stage each named failure the
    issue calls out."""

    COMPONENTS = ({'name': 'digital-input:12', 'kind': 'digital-input'},
                  {'name': 'motor:21', 'kind': 'motor'})
    BOUND = {'digital-input:12': {302}, 'motor:21': {302}}
    KINDS = {302: 'bool', 10: 'float'}  # the point kinds /signals declares
    WRITABLE = {302}

    def __init__(self):
        self.tick = 0
        self.next_seq = 1
        # The pair's mutable halves: each peer's reported role and its
        # own journal — the dcs-ctl switch legs move the roles and each
        # peer audits its own role_changed entries.
        self.roles = {'ctrl-a:1': 'standby', 'ctrl-b:2': 'active'}
        self.journals = {'ctrl-a:1': [], 'ctrl-b:2': []}
        # The served plant state the mutation legs observe: point
        # values, standing forces, and live parameter values.
        self.values = {302: {'bool': False}, 10: {'float': 2.5}}
        self.forces = {}
        self.params = {'motor:21': {'gain': {'float': 5.0}}}
        self.paced = True            # the rig's monitor paces scans
        self.receipts_log = []  # the executor's settled-receipt log
        self.calls = []      # the recorded (addr, argv) transcript
        self.binaries = []   # the binary path each invocation ran
        # Fault injection for the named-failure cases.
        self.role_mismatch = False     # the CLI's role reads both standby
        self.missing_kind = False      # the schema read drops an instance
        self.resources_misses = False  # the resources view drops an instance
        self.resources_entry_wrong = False  # a named entry mismatches
        self.events_map_broken = False  # the keyed events view drops a key
        self.receipts_missing = False  # the receipt log loses the invoke
        self.history_empty = False     # the point retains no samples
        self.no_bound_measurement = False  # no measurement binds a point
        self.command_fails = False     # receipted submissions die transport-side
        self.no_journal_entry = False  # the settlement never journals
        self.wrong_actor = False       # the journaled receipt loses the actor
        self.no_events = False         # the events read attributes nothing
        self.silent_accept = False     # the undeclared invoke applies
        self.carryover = None          # a receipt an adopted checkpoint
                                       # re-journals above the cursor
        # Fault injection for the write-side legs.
        self.no_writable_point = False   # the index drops p101-oos's flag
        self.no_tune_param = False       # no float tunable is declared
        self.snapshot_fails = False      # the snapshot read dies
        self.write_never_serves = False  # an applied write never serves
        self.tune_never_serves = False   # a tuned value never reports
        self.force_no_badge = False      # the force never badges
        self.unforce_sticks = False      # the release never clears
        self.standby_accepts = False     # the standby admits commands
        self.demote_refused = False      # demote meets a named refusal
        self.promote_refused = False     # promote meets a named refusal
        self.roles_stuck = False         # the switch report never settles
        self.role_events_missing = False  # no role_changed is journaled
        self.leg_settlements_dropped = False  # leg settlements never journal
        self.wrong_leg_actor = False     # leg settlements journal foreign

    @property
    def journal(self):
        """The currently-active peer's journal — where the tests' seeded
        and the receipted legs' settled entries land."""
        active = next((addr for addr, role in self.roles.items()
                       if role == 'active'), 'ctrl-b:2')
        return self.journals[active]

    @staticmethod
    def _ok(payload):
        return json.dumps(payload), 0, ''

    # The subprocess seam — replaces scenarios._run_ctl: one argv
    # answered the way the real binary would against this fake rig.
    def run_ctl(self, binary, addr, args):
        self.binaries.append(binary)
        self.calls.append((addr, list(args)))
        stdout, rc, stderr = self._dispatch(addr, list(args))
        return CtlResult(stdout=stdout, stderr=stderr, returncode=rc)

    def _dispatch(self, addr, args):
        self.tick += 1
        if args == ['role']:
            role = 'standby' if self.role_mismatch \
                else self.roles.get(addr, 'standby')
            return self._ok({'role': role, 'tick': self.tick})
        if args == ['signals']:
            return self._ok(self._signals())
        if args == ['schema']:
            return self._ok({'publication': self.tick,
                             'tick': self.tick,
                             'interfaces': self._interfaces()})
        if args == ['snapshot']:
            if self.snapshot_fails:
                return '', 1, 'dcs-ctl: ' + addr + ': connection refused'
            return self._ok(self._snapshot())
        if args == ['demote'] or args == ['promote']:
            return self._switch(addr, args[0] == 'promote')
        if args[0] == 'scan':
            if self.paced:
                return '', 1, 'dcs-ctl: ' + addr + ': HTTP 409: ' \
                    'refused: scans are paced to wall-clock time by ' \
                    'this instance'
            self.tick += int(args[1])
            return self._ok(self._snapshot())
        if args[0] == 'journal':
            since = int(args[args.index('--since') + 1]) \
                if '--since' in args else 0
            return self._ok([entry for entry
                             in self.journals.get(addr, [])
                             if entry['seq'] > since])
        if args[0] == 'events':
            if len(args) == 1:
                keyed = {record['name']: self._events_for(record['name'])
                         for record in self.COMPONENTS}
                if self.events_map_broken:
                    keyed.pop('motor:21', None)
                return self._ok(keyed)
            entry = self._view_entry(args[1])
            if entry is None:
                return '', 1, 'dcs-ctl: ' + addr \
                    + ': no served component named ' + repr(args[1])
            return self._ok(entry['events'])
        if args[0] == 'resources':
            if len(args) == 1:
                return self._ok(self._view())
            entry = self._view_entry(args[1])
            if entry is None:
                return '', 1, 'dcs-ctl: ' + addr \
                    + ': no served component named ' + repr(args[1])
            return self._ok(entry)
        if args == ['receipts']:
            return self._ok(self.receipts_log)
        if args[0] == 'history':
            points = [int(args[index + 1])
                      for index, arg in enumerate(args)
                      if arg == '--point']
            return self._ok([{'point': point,
                              'samples': self._samples(point)}
                             for point in points])
        # Receipted subcommands — `write`, `set-parameter`, `force`,
        # `unforce`, `invoke`: the receipt prints to stdout; a named
        # rejection exits 1.
        actor = None
        if '--actor' in args:
            index = args.index('--actor')
            actor = args[index + 1]
            del args[index:index + 2]
        if self.command_fails:
            return '', 1, 'dcs-ctl: ' + addr + ': connection refused'
        command = self._command_from_args(args)
        refused = self._refusal(command, addr)
        if refused is None:
            receipt = {'command': command,
                       'outcome': {'accepted':
                                   {'apply_tick': self.tick + 1}},
                       'actor': actor}
            rc, stderr = 0, ''
        else:
            receipt = {'command': command,
                       'outcome': {'rejected': {'reason': refused}},
                       'actor': actor}
            rc = 1
            stderr = 'dcs-ctl: ' + addr + ': command rejected: ' \
                + next(iter(refused))
        # An accepted command journals its applied echo at the promised
        # tick; a rejected receipt is already final and echoes verbatim —
        # the real executor's durable record. The receipt log mirrors
        # the same settled outcome. `carryover` injects the
        # entry a checkpoint-adopted receipt re-journals on this peer —
        # an earlier leg's identical command under its own actor —
        # landing above the consumer's pre-submission cursor, ahead of
        # this submission's own settlement.
        settled = dict(receipt)
        if refused is None:
            settled['outcome'] = {'applied': {'tick': self.tick + 1}}
            self._apply(command)
        leg_actor = isinstance(actor, str) \
            and actor.startswith('qa-lane-dcs-ctl-leg-')
        if self.wrong_actor \
                or (self.wrong_leg_actor and leg_actor):
            settled['actor'] = 'qa-lane'
        if not self.receipts_missing:
            self.receipts_log.append(settled)
        if not self.no_journal_entry \
                and not (self.leg_settlements_dropped and leg_actor):
            if self.carryover is not None:
                self.journals[addr].append(
                    {'seq': self.next_seq, 'tick': self.tick,
                     'event': {'command_settled': {
                         'receipt': self.carryover}}})
                self.next_seq += 1
                self.carryover = None
            self.journals[addr].append(
                {'seq': self.next_seq, 'tick': self.tick,
                 'event': {'command_settled': {'receipt': settled}}})
            self.next_seq += 1
        return json.dumps(receipt), rc, stderr

    def _apply(self, command):
        """The applied half of an admitted submission — the served
        snapshot and parameter report follow it."""
        write = command.get('write_value')
        if write is not None and not self.write_never_serves:
            self.values[write['point']] = write['value']
        tune = command.get('set_parameter')
        if tune is not None and not self.tune_never_serves:
            self.params.setdefault(tune['component'], {})[
                tune['name']] = tune['value']
        force = command.get('force_point')
        if force is not None and not self.force_no_badge:
            self.forces[force['point']] = force['value']
        unforce = command.get('unforce_point')
        if unforce is not None and not self.unforce_sticks:
            self.forces.pop(unforce['point'], None)

    def _journal_role(self, addr, from_role, to_role):
        """A peer's own role_changed audit entry — each peer journals
        its transitions on its own journal."""
        if self.role_events_missing:
            return
        self.journals[addr].append(
            {'seq': self.next_seq, 'tick': self.tick,
             'event': {'role_changed': {'from': from_role,
                                        'to': to_role}}})
        self.next_seq += 1

    def _switch(self, addr, promote):
        """`dcs-ctl promote`/`demote`: a named SwitchError refusal on
        stderr with exit 1, else the transitional RoleReport — the
        role itself settles at the next reported read."""
        role = self.roles.get(addr, 'standby')
        if promote:
            if role != 'standby':
                return '', 1, 'dcs-ctl: ' + addr + ': promote ' \
                    'refused: already_active: instance already owns ' \
                    'field writes (role active or promoting)'
            if self.promote_refused:
                return '', 1, 'dcs-ctl: ' + addr + ': promote ' \
                    'refused: not_converged: standby has not ' \
                    'converged: unsynchronized'
            if not self.roles_stuck:
                self.roles[addr] = 'active'
            self._journal_role(addr, 'standby', 'promoting')
            self._journal_role(addr, 'promoting', 'active')
            return self._ok({'role': 'promoting', 'tick': self.tick,
                             'sync': {'tracking':
                                      {'aligned': self.tick}}})
        if role != 'active':
            return '', 1, 'dcs-ctl: ' + addr + ': demote refused: ' \
                'not_active: instance does not own field writes ' \
                '(role standby or demoting)'
        if self.demote_refused:
            return '', 1, 'dcs-ctl: ' + addr + ': demote refused: ' \
                'no_tracking_source: no checkpoint source is ' \
                'configured or announced'
        if not self.roles_stuck:
            self.roles[addr] = 'standby'
        self._journal_role(addr, 'active', 'demoting')
        self._journal_role(addr, 'demoting', 'standby')
        return self._ok({'role': 'demoting', 'tick': self.tick,
                         'sync': {'tracking': {'aligned': self.tick}}})

    def _snapshot(self):
        """The TelemetrySnapshot the binary's snapshot read prints —
        the point values under any standing forces, the forces badges,
        and the live parameter report."""
        points = []
        for point, value in sorted(self.values.items()):
            forced = point in self.forces
            points.append({'point': point, 'direction': 'in',
                           'sample': {
                               'value': self.forces.get(point, value),
                               'quality': {'uncertain': 'substituted'}
                               if forced else 'good',
                               'tick': self.tick, 'seq': self.tick}})
        return {'tick': self.tick, 'points': points,
                'forces': [{'point': point, 'value': value}
                           for point, value
                           in sorted(self.forces.items())],
                'parameters': [{'name': name, 'values': dict(vals)}
                               for name, vals
                               in sorted(self.params.items())],
                'descriptors': [], 'components': [],
                'publication': {'published': self.tick, 'depth': 8}}

    @staticmethod
    def _literal(text):
        if text == 'true':
            return {'bool': True}
        if text == 'false':
            return {'bool': False}
        try:
            return {'int': int(text)}
        except ValueError:
            return {'float': float(text)}

    def _command_from_args(self, args):
        """The Command a receipted argv denotes — write/set-parameter/
        invoke with the declared point kind the real binary reads out
        of the signal index."""
        if args[0] == 'write':
            point = int(args[1])
            value = self._literal(args[2])
            kind = self.KINDS.get(point) or next(iter(value))
            return {'write_value': {'point': point, 'kind': kind,
                                    'value': value}}
        if args[0] == 'set-parameter':
            return {'set_parameter': {'component': args[1],
                                      'name': args[2],
                                      'value': self._literal(args[3])}}
        if args[0] == 'force':
            point = int(args[1])
            value = self._literal(args[2])
            kind = self.KINDS.get(point) or next(iter(value))
            return {'force_point': {'point': point, 'kind': kind,
                                    'value': value}}
        if args[0] == 'unforce':
            return {'unforce_point': {'point': int(args[1])}}
        if args[0] == 'invoke':
            arguments = {}
            for pair in args[3:]:
                name, _, text = pair.partition('=')
                arguments[name] = self._literal(text)
            return {'invoke': {'component': args[1],
                               'command': args[2],
                               'arguments': arguments}}
        raise AssertionError('unexpected dcs-ctl argv %s' % args)

    def _refusal(self, command, addr):
        """The named rejection a submission meets — mirroring the
        executor's validation order (the role gate ahead of the
        command's own checks) — or None when it is admitted."""
        if self.roles.get(addr) != 'active' and not self.standby_accepts:
            point = (command.get('write_value') or {}).get('point') \
                or (command.get('force_point') or {}).get('point') \
                or (command.get('unforce_point') or {}).get('point')
            return {'not_active': {'point': point,
                                   'role': self.roles.get(addr)
                                   or 'standby'}}
        write = command.get('write_value')
        if write is not None and write['point'] not in self.WRITABLE:
            return {'not_writable': {'point': write['point']}}
        force = command.get('force_point')
        if force is not None and force['point'] not in self.WRITABLE:
            return {'not_writable': {'point': force['point']}}
        invoke = command.get('invoke')
        if invoke is not None:
            interface = self._served().get(invoke['component'])
            if interface is None:
                return {'unknown_component':
                        {'component': invoke['component']}}
            names = {spec['name'] for spec in interface['commands']}
            if invoke['command'] not in names \
                    and not self.silent_accept:
                return {'unknown_command': {
                    'component': invoke['component'],
                    'command': invoke['command']}}
        return None

    def _interface(self, kind):
        """One kind's declared interface — a bound measurement, a
        writable-bool write, an unavailable non-writable write (the
        unavailable probe's target), a parameter tune, and the emitted
        settled-event entry."""
        measurement = {'name': 'in', 'kind': 'bool'}
        if not self.no_bound_measurement:
            measurement['point'] = 302
        configuration = []
        if kind == 'motor' and not self.no_tune_param:
            configuration = [{'name': 'gain', 'kind': 'float',
                              'capability': 'tunable',
                              'range': {'min': {'float': 0.0},
                                        'max': {'float': 10.0}}}]
        return {'version': 1, 'kind': kind,
                'measurements': [measurement],
                'configuration': configuration, 'state': [],
                'commands': [
                    {'name': 'write_value:in', 'point': 302,
                     'request': [{'name': 'value', 'kind': 'bool'}],
                     'availability': 'bound_point_writable',
                     'adapted': 'write_value'},
                    {'name': 'write_value:raw', 'point': 10,
                     'request': [{'name': 'value', 'kind': 'float'}],
                     'availability': 'bound_point_writable',
                     'adapted': 'write_value'},
                    {'name': 'set_parameter:invert',
                     'request': [{'name': 'value', 'kind': 'bool'}],
                     'availability': 'always',
                     'adapted': 'set_parameter'}],
                'events': [{'name': 'command_settled', 'payload': [],
                            'retention': 'journal',
                            'emission': 'on_command_settled',
                            'adapted': 'command_settled'}]}

    def _interfaces(self):
        interfaces = [{'name': record['name'],
                       'interface': self._interface(record['kind'])}
                      for record in self.COMPONENTS]
        return interfaces[:1] if self.missing_kind else interfaces

    def _served(self):
        return {entry['name']: entry['interface']
                for entry in self._interfaces()}

    def _signals(self):
        return {'points': [
            {'point': 302, 'signal': 10302, 'name': 'p101-oos',
             'direction': 'in', 'value_type': 'bool',
             'writable': not self.no_writable_point},
            {'point': 10, 'signal': 10010, 'name': 'level-primary',
             'direction': 'in', 'value_type': 'float',
             'writable': False}],
            'components': [dict(record) for record in self.COMPONENTS]}

    def _resources(self, record):
        interface = self._interface(record['kind'])
        commands = []
        for spec in interface['commands']:
            available = spec.get('adapted') != 'write_value' \
                or spec.get('point') in self.WRITABLE
            commands.append({'name': spec['name'], 'available': available,
                             'refusal': None if available else
                             'point ' + str(spec.get('point'))
                             + ' is not writable'})
        measurements = [{'name': spec['name'], 'point': spec.get('point'),
                         'sample': {'value': {'bool': True},
                                    'quality': {'good': {}},
                                    'tick': self.tick}}
                        for spec in interface['measurements']]
        configuration = [{'name': spec['name'],
                          'value': (self.params.get(record['name'])
                                    or {}).get(spec['name'])}
                         for spec in interface['configuration']]
        entry = {'name': record['name'], 'kind': record['kind'],
                 'measurements': measurements,
                 'configuration': configuration,
                 'state': [], 'commands': commands,
                 'events': self._events_for(record['name'])}
        if self.resources_entry_wrong:
            entry['commands'] = []
        return entry

    def _view(self):
        """The served ResourceView every resources/events read derives
        from — one live record per component instance."""
        records = self.COMPONENTS[:1] if self.resources_misses \
            else self.COMPONENTS
        return {'publication': self.tick, 'tick': self.tick,
                'components': [self._resources(record)
                               for record in records]}

    def _view_entry(self, name):
        """The named instance's ComponentResources — the lookup
        `resources <component>`/`events <component>` share."""
        for record in self.COMPONENTS:
            if record['name'] == name:
                return self._resources(record)
        return None

    def _events_for(self, name):
        """One instance's attributed events — the retained journal
        tail the served view attributes to it."""
        if self.no_events:
            return []
        return [entry for entry in self.journal
                if self._attributed(entry, name)]

    def _samples(self, point):
        """The retained samples a `history --point` read answers for
        a declared point."""
        if self.history_empty or point not in self.KINDS:
            return []
        value = {'bool': True} if self.KINDS[point] == 'bool' \
            else {'float': 1.0}
        return [{'seq': seq,
                 'sample': {'value': value, 'quality': {'good': {}},
                            'tick': seq}}
                for seq in (1, 2, 3)]

    def _attributed(self, entry, name):
        """The per-component events attribution, mirroring the served
        view's rule: commands settle against their component or their
        bound point; emitted events name their producer."""
        event = entry.get('event') or {}
        bound = self.BOUND.get(name, set())
        emitted = event.get('event_emitted') or {}
        if emitted.get('component') == name:
            return True
        receipt = (event.get('command_settled') or {}) \
            .get('receipt') or {}
        command = receipt.get('command') or {}
        component = (command.get('invoke') or {}).get('component') \
            or (command.get('set_parameter') or {}).get('component') \
            or (command.get('force') or {}).get('component')
        point = (command.get('write_value') or {}).get('point')
        return component == name or point in bound

    # The raw channel the scenario still crosses — the pair's liveness
    # gate; every served-resource read rides the binary seam now.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.roles.get(host, 'standby'),
                         'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))


class DcsCtlTests(unittest.TestCase):
    """scenario_dcs_ctl behind the faked-subprocess seam: CtlFeed
    models the binary's argv contract so every pass/fail leg the issue
    calls out — role layout, schema coverage, the journaled and
    actor-attributed settlement, event attribution, the named refusals,
    the mutation legs' per-verb assertions and rejection paths, role
    restoration, and an unavailable binary — runs through the real
    scenario path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.binary = Path(self.tmp.name) / 'dcs-ctl'
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o755)
        self.feed = CtlFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'dcs_ctl': str(self.binary)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_run_ctl', self.feed.run_ctl), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CTL_DEADLINE', 0.5):
            return scenarios.scenario_dcs_ctl(ctx)

    def assert_scenario(self, outcome, detail_part):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], outcome, record)
        self.assertIn(detail_part, record.get('detail', ''), record)
        report.validate_scenario(record)
        return record

    def test_registered_last_and_replayable(self):
        self.assertIs(scenarios.SCENARIOS[-1],
                      scenarios.scenario_dcs_ctl)
        self.assertIs(verify.case_function('dcs-ctl'),
                      scenarios.scenario_dcs_ctl)

    def test_clean_rig_passes_with_full_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        for name in ('dcs-ctl-roles', 'dcs-ctl-schema',
                     'dcs-ctl-resources', 'dcs-ctl-invoke',
                     'dcs-ctl-journal', 'dcs-ctl-events',
                     'dcs-ctl-receipts', 'dcs-ctl-history',
                     'dcs-ctl-refusals', 'dcs-ctl-transcript'):
            self.assertTrue((self.evidence / (name + '.json')).is_file(),
                            name)
        transcript = json.loads(
            (self.evidence / 'dcs-ctl-transcript.json').read_text())
        argvs = [entry['argv'] for entry in transcript]
        # The consumer contract end to end: role on both endpoints, the
        # served-resource reads, the picked command with the declared
        # actor, the journal and events reads, receipts and history,
        # and both refusal probes.
        self.assertIn(['ctrl-a:1', 'role'], argvs)
        self.assertIn(['ctrl-b:2', 'role'], argvs)
        self.assertIn(['ctrl-b:2', 'resources'], argvs)
        self.assertIn(['ctrl-b:2', 'resources', 'digital-input:12'],
                      argvs)
        self.assertIn(['ctrl-b:2', 'write', '302', 'true',
                       '--actor', scenarios.CTL_ACTOR], argvs)
        self.assertIn(['ctrl-b:2', 'events'], argvs)
        self.assertIn(['ctrl-b:2', 'events', 'digital-input:12'], argvs)
        self.assertIn(['ctrl-b:2', 'receipts'], argvs)
        self.assertIn(['ctrl-b:2', 'history', '--point', '302'], argvs)
        self.assertIn(['ctrl-b:2', 'invoke', 'digital-input:12',
                       'dcs-ctl-undeclared', '--actor',
                       scenarios.CTL_ACTOR], argvs)
        self.assertIn(['ctrl-b:2', 'write', '10', '1.0',
                       '--actor', scenarios.CTL_ACTOR], argvs)
        self.assertTrue(self.feed.binaries)
        self.assertTrue(all(binary == str(self.binary)
                            for binary in self.feed.binaries))
        refusals = json.loads(
            (self.evidence / 'dcs-ctl-refusals.json').read_text())
        self.assertEqual(refusals['undeclared']['exit'], 1)
        self.assertEqual(refusals['unavailable']['exit'], 1)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        first = {path.name: path.read_text()
                 for path in self.evidence.iterdir()}
        self.evidence = self.evidence.parent / 'evidence-2'
        self.evidence.mkdir()
        self.feed = CtlFeed()
        again = self.run_scenario()
        second = {path.name: path.read_text()
                  for path in self.evidence.iterdir()}
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(again['outcome'], 'passed', again)
        self.assertEqual(first, second)

    def test_role_mismatch_fails(self):
        self.feed.role_mismatch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('one standby', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_schema_kind_fails(self):
        self.feed.missing_kind = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('misses declared kinds', record.get('detail', ''))
        self.assertIn('motor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_resources_missing_component_fails(self):
        self.feed.resources_misses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('one kind-matched record per declared component',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_resources_entry_mismatch_fails(self):
        self.feed.resources_entry_wrong = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('does not mirror the served interface',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_keyed_events_missing_component_fails(self):
        self.feed.events_map_broken = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('keyed events read', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipts_missing_invoke_fails(self):
        self.feed.receipts_missing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt log never recorded',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_history_without_samples_fails(self):
        self.feed.history_empty = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('retains no served samples',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_bound_measurement_is_inconclusive(self):
        self.feed.no_bound_measurement = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no served measurement binds a point',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_receipt_fails(self):
        self.feed.command_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no settled receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_settlement_fails(self):
        self.feed.no_journal_entry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never recorded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_actor_fails(self):
        self.feed.wrong_actor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unattributed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_stale_identical_settlement_is_not_this_legs(self):
        """The qa-20260916-065 defect: the served-interface case
        submits the same picked command under actor 'qa-lane' ahead of
        this leg, so the journal already holds an identical settled
        receipt — the settlement read must start above the
        pre-submission seq cursor, not match the stale entry."""
        stale = {'seq': self.feed.next_seq, 'tick': 0,
                 'event': {'command_settled': {'receipt': {
                     'command': {'write_value': {
                         'point': 302, 'kind': 'bool',
                         'value': {'bool': True}}},
                     'outcome': {'applied': {'tick': 0}},
                     'actor': 'qa-lane'}}}}
        self.feed.journal.append(stale)
        self.feed.next_seq += 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        journal = json.loads(
            (self.evidence / 'dcs-ctl-journal.json').read_text())
        self.assertEqual(
            journal['entry']['event']['command_settled']['receipt']
            ['actor'], scenarios.CTL_ACTOR)

    def test_carried_over_settlement_is_not_this_legs(self):
        """The qa-20260917-002 defect: an earlier leg submits the same
        picked command under actor 'qa-lane'; the receipt crosses peers
        inside a checkpoint and re-journals on the serving peer only
        when the adopting scan records it — landing above this leg's
        pre-submission seq cursor, ahead of this submission's own
        settlement. The settlement read must identify this leg's
        receipt by the actor only it declares, not take the first
        same-command entry past the floor."""
        self.feed.carryover = {
            'command': {'write_value': {'point': 302, 'kind': 'bool',
                                        'value': {'bool': True}}},
            'outcome': {'applied': {'tick': 40}},
            'actor': 'qa-lane'}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        journal = json.loads(
            (self.evidence / 'dcs-ctl-journal.json').read_text())
        entry = journal['entry']['event']['command_settled']['receipt']
        self.assertEqual(entry['actor'], scenarios.CTL_ACTOR)
        self.assertEqual(
            journal['foreign']['event']['command_settled']['receipt']
            ['actor'], 'qa-lane')

    def test_unattributed_event_fails(self):
        self.feed.no_events = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('attributes no produced event',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_silently_accepted_undeclared_invoke_fails(self):
        self.feed.silent_accept = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not refused by name', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unavailable_binary_is_inconclusive(self):
        stub = Path(self.tmp.name) / 'stub-ctl'
        stub.write_text('')   # present but not executable
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        for value in (None, str(Path(self.tmp.name) / 'no-such-ctl'),
                      str(stub)):
            if value is None:
                ctx.pop('dcs_ctl', None)
            else:
                ctx['dcs_ctl'] = value
            record = scenarios.scenario_dcs_ctl(ctx)
            self.assertEqual(record['outcome'], 'inconclusive',
                             (value, record))
            report.validate_scenario(record)

    def test_mutation_legs_cover_each_write_verb(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        transcript = json.loads((self.evidence
                                 / 'dcs-ctl-transcript.json').read_text())
        argvs = [entry['argv'] for entry in transcript]
        actor = scenarios.CTL_ACTOR
        # Every write-side verb went through the binary — write,
        # set-parameter, force, unforce, demote, promote, and the
        # paced-mode scan attempt — each receipted leg under its own
        # per-leg actor (demote/promote/scan carry none per the
        # binary's documented contract).
        self.assertIn(['ctrl-b:2', 'write', '10', '1.0',
                       '--actor', actor + '-leg-1'], argvs)
        self.assertIn(['ctrl-a:1', 'write', '302', 'false',
                       '--actor', actor + '-leg-2'], argvs)
        self.assertIn(['ctrl-b:2', 'write', '302', 'false',
                       '--actor', actor + '-leg-3'], argvs)
        self.assertIn(['ctrl-b:2', 'set-parameter', 'motor:21', 'gain',
                       '1.0', '--actor', actor + '-leg-4'], argvs)
        self.assertIn(['ctrl-b:2', 'force', '302', 'false',
                       '--actor', actor + '-leg-5'], argvs)
        self.assertIn(['ctrl-b:2', 'unforce', '302',
                       '--actor', actor + '-leg-6'], argvs)
        self.assertIn(['ctrl-b:2', 'demote'], argvs)
        self.assertIn(['ctrl-a:1', 'promote'], argvs)
        self.assertIn(['ctrl-a:1', 'scan', '1'], argvs)
        # Per-leg evidence landed under the run's evidence directory.
        for leg in ('write-nonwritable', 'standby-directed', 'write',
                    'set-parameter', 'force', 'unforce', 'demote',
                    'promote', 'scan'):
            self.assertTrue((self.evidence
                             / ('dcs-ctl-leg-' + leg + '.json')
                             ).is_file(), leg)
        self.assertTrue((self.evidence
                         / 'dcs-ctl-write-legs.json').is_file())
        # The pair's pre-scenario roles and the commanded state are
        # restored for later scenarios.
        self.assertEqual(self.feed.roles,
                         {'ctrl-a:1': 'standby', 'ctrl-b:2': 'active'})
        self.assertEqual(self.feed.values[302], {'bool': True})
        self.assertEqual(self.feed.forces, {})
        self.assertEqual(self.feed.params['motor:21']['gain'],
                         {'float': 5.0})

    def test_mutation_leg_payloads_show_attribution(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        actor = scenarios.CTL_ACTOR
        write = json.loads((self.evidence
                            / 'dcs-ctl-leg-write.json').read_text())
        self.assertEqual(write['receipt']['actor'], actor + '-leg-3')
        self.assertIn('applied', write['settled']['outcome'])
        self.assertEqual(write['settled']['actor'], actor + '-leg-3')
        tune = json.loads((self.evidence
                           / 'dcs-ctl-leg-set-parameter.json')
                          .read_text())
        self.assertEqual(tune['receipt']['actor'], actor + '-leg-4')
        self.assertIn('applied', tune['settled']['outcome'])
        force = json.loads((self.evidence
                            / 'dcs-ctl-leg-force.json').read_text())
        self.assertEqual(force['receipt']['actor'], actor + '-leg-5')
        self.assertIn('applied', force['settled']['outcome'])
        unforce = json.loads((self.evidence
                              / 'dcs-ctl-leg-unforce.json').read_text())
        self.assertEqual(unforce['receipt']['actor'], actor + '-leg-6')
        self.assertIn('applied', unforce['settled']['outcome'])
        demote = json.loads((self.evidence
                             / 'dcs-ctl-leg-demote.json').read_text())
        self.assertEqual(demote['report']['role'], 'demoting')
        self.assertEqual(demote['settled']['role'], 'standby')
        self.assertEqual(demote['role_changed']['event']
                         ['role_changed']['to'], 'standby')
        promote = json.loads((self.evidence
                              / 'dcs-ctl-leg-promote.json').read_text())
        self.assertEqual(promote['report']['role'], 'promoting')
        self.assertEqual(promote['settled']['role'], 'active')
        self.assertEqual(promote['role_changed']['event']
                         ['role_changed']['to'], 'active')

    def test_mutation_rejection_legs_name_their_refusals(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        nonwritable = json.loads(
            (self.evidence / 'dcs-ctl-leg-write-nonwritable.json')
            .read_text())
        self.assertEqual(nonwritable['exit'], 1)
        self.assertIn('not_writable', nonwritable['stderr'])
        self.assertIn('not_writable', nonwritable['receipt']['outcome']
                      ['rejected']['reason'])
        standby = json.loads(
            (self.evidence / 'dcs-ctl-leg-standby-directed.json')
            .read_text())
        self.assertEqual(standby['exit'], 1)
        self.assertIn('not_active', standby['stderr'])
        self.assertIn('not_active', standby['receipt']['outcome']
                      ['rejected']['reason'])
        self.assertEqual(standby['peer'], 'active')
        # Neither rejection perturbed plant state — the writable point
        # still serves what the read-side leg wrote.
        self.assertEqual(self.feed.values[302], {'bool': True})

    def test_paced_scan_leg_records_a_skipped_attempt(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        scan = json.loads((self.evidence
                           / 'dcs-ctl-leg-scan.json').read_text())
        self.assertEqual(scan['exit'], 1)
        self.assertIn('paced', scan['stderr'])
        self.assertIn('paced', scan['skipped'])

    def test_driven_scan_leg_verifies_served_tick(self):
        self.feed.paced = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        scan = json.loads((self.evidence
                           / 'dcs-ctl-leg-scan.json').read_text())
        self.assertEqual(scan['exit'], 0)
        self.assertIsInstance(scan['answer']['tick'], int)

    def test_unserved_write_fails(self):
        self.feed.write_never_serves = True
        self.assert_scenario('failed', 'written value never surfaced')

    def test_unserved_tune_fails(self):
        self.feed.tune_never_serves = True
        self.assert_scenario('failed', 'tuned value never surfaced')

    def test_unbadged_force_fails(self):
        self.feed.force_no_badge = True
        self.assert_scenario('failed', 'never served the substituted')

    def test_stuck_unforce_fails(self):
        self.feed.unforce_sticks = True
        self.assert_scenario('failed', 'badge never cleared')

    def test_standby_accepting_commands_fails(self):
        self.feed.standby_accepts = True
        self.assert_scenario('failed', 'not refused by name')

    def test_demote_refusal_fails_and_restores(self):
        self.feed.demote_refused = True
        self.assert_scenario('failed', 'transitional RoleReport')
        # The demote was refused, so the pair never left its
        # pre-scenario roles and restoration is a no-op.
        self.assertEqual(self.feed.roles,
                         {'ctrl-a:1': 'standby', 'ctrl-b:2': 'active'})

    def test_promote_refusal_fails(self):
        self.feed.promote_refused = True
        self.assert_scenario('failed', 'transitional RoleReport')

    def test_unsettled_switch_fails(self):
        self.feed.roles_stuck = True
        self.assert_scenario('failed', 'never settled')

    def test_missing_role_events_fail(self):
        self.feed.role_events_missing = True
        self.assert_scenario('failed', 'role_changed transitions')

    def test_unsettled_write_leg_fails(self):
        self.feed.leg_settlements_dropped = True
        self.assert_scenario('failed', 'never settled the write leg')

    def test_foreign_leg_actor_fails(self):
        self.feed.wrong_leg_actor = True
        self.assert_scenario('failed', 'never settled the write leg')

    def test_no_writable_bool_is_inconclusive(self):
        self.feed.no_writable_point = True
        self.assert_scenario('inconclusive', 'no writable bool p101-oos')

    def test_no_tunable_parameter_is_inconclusive(self):
        self.feed.no_tune_param = True
        self.assert_scenario('inconclusive', 'no descriptor-declared')

    def test_snapshot_failure_is_inconclusive(self):
        self.feed.snapshot_fails = True
        self.assert_scenario('inconclusive', 'snapshot failed')


if __name__ == '__main__':
    unittest.main()
