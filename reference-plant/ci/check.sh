#!/usr/bin/env bash
# The reference plant's clean-CI check: everything a fresh clone of this
# repository runs with no platform checkout present.
#
# Stages, each reporting the release contract's named diagnostics on
# failure (docs/release-contract.md):
#
#   resolve      cargo fetch — the pinned release crates resolve
#                (pin-unresolvable)
#   build        cargo build — the composition compiles against the
#                supported surface (surface-incompatible)
#   lockfile     Cargo.lock records only git sources for the release
#                crates — never a path into a checkout
#                (path-dependency-leak)
#   emit         the model and scenario emit byte-identically twice and
#                match the checked-in artifacts (emit-nondeterministic,
#                stale-artifact)
#   tooling      the released tooling accepts the emitted model —
#                `dcs-model validate`, `dcs-model lint`,
#                `dcs-controller --check` — and the dynamics document
#                standalone, `dcs-plant-server --check-dynamics` merging
#                and validating every element against the model's
#                channel map with no server launched — and exercises the
#                contract's
#                remaining dcs-model surfaces: `dcs-model schema` and
#                `dcs-model interface-schema` emissions byte-identical
#                to the release record's schema artifacts (fetched from
#                the pinned revision through the same git remote the
#                pins resolve over), `dcs-model diff` naming a doctored
#                compatible revision's changes and none on the
#                identical document, and `dcs-model summary` /
#                `dcs-model signal-index` outputs recorded to the run's
#                evidence (tooling-rejected, pin-unresolvable,
#                schema-drift, diff-mismatch)
#   alarm-validation
#                the rejection half of decision 70's alarm record at
#                the customer boundary — every managed alarm instance
#                carrying its rationalization prose and
#                priority/class/response_ticks codes, doctored copies
#                refused by the released `dcs-controller --check`
#                naming the missing element, and the driven run's
#                served components/parameters sections reporting the
#                same record (alarm-validation-failed,
#                alarm-validation-nondeterministic)
#   fingerprint  the emitted model's fingerprint equals the manifest's
#                recorded `model.fingerprint`, the deployed pair's
#                served model digest — each peer's /checkpoint-stamped
#                fingerprint on the manifest-declared deployment —
#                equals it too, so the record authorizes the served
#                model bytes rather than only the checked-in file; a
#                doctored served document — every point id renumbered
#                over identical components — reports the named
#                mismatch carrying the expected vs served fingerprint
#                and the first diverging section; and the dynamics
#                document the manifest-declared deployment serves —
#                the `dynamics.path` the rig mounts and
#                `dcs-plant-server --dynamics` merges — fingerprints
#                the recorded optional `dynamics.fingerprint` and the
#                checked-in artifact identically on the launched
#                pair, a doctored served document with renumbered
#                point references over identical element content
#                reporting the named mismatch; two passes produce
#                identical digests (manifest-fingerprint-mismatch,
#                fingerprint-failed, fingerprint-nondeterministic,
#                fingerprint-unchecked, dynamics-fingerprint-failed,
#                dynamics-fingerprint-nondeterministic,
#                dynamics-fingerprint-unchecked)
#   deploy       the checked-in rig definition deploy/compose.yaml
#                instantiates every field of deploy/manifest.json —
#                release, images, mounted model and dynamics paths,
#                the fingerprint propagated into the controller
#                invocations, listen addresses, the pair's standby
#                wiring, the standby's optional failover_budget
#                declaration carried as its --auto-promote flag, and
#                the optional per-controller persistence
#                paths (state_file/journal_file) backed by writable
#                mounts and flags, and the optional topology
#                section's declared pairs — each naming two
#                declared members whose standby wiring closes
#                inside the pair — parsed and validated through
#                `docker compose config` or the fallback parser, with
#                the fields' divergence cases exercised against
#                doctored copies
#                (rig-invalid, rig-unverifiable, rig-mismatch)
#   simulate     the scripted simulation's declared outcomes hold, and
#                two runs produce identical digests (scenario-failed,
#                scenario-nondeterministic)
#   restart      the restart-recovery leg (WW-LCM-001's
#                lone-controller clause): the field-owning controller
#                runs the deterministic scenario on the
#                manifest-declared --state-file/--journal-file flags
#                pointed at runner-owned scratch paths, is stopped at
#                a leg boundary, and relaunches onto the same files —
#                the resumed run must continue at the persisted tick
#                with leg outcomes, receipts, and the field image
#                equal to an uninterrupted reference pass, the
#                journal's seq order continuing across the file's
#                run-boundary marker; a missing or unparseable state
#                file never passes silently
#                (restart-resume-failed, restart-resume-nondeterministic)
#   surface      the served operator surface — the signal index, the
#                monitoring page, the snapshot's descriptors, the
#                block-interface registry covering every declared
#                component, the kind-declared commands answering
#                structured receipts through POST /command and
#                reporting their published availability verdicts
#                through GET /resources in both directions, and the
#                kind-emitted events reaching the journal and the
#                per-instance resource view — matches the emitted
#                model's declaration, in the same deterministic
#                --driven run the simulate stage performs — plus the
#                served GET /schema document's structural conformance
#                to the fetched block-interfaces schema artifact
#                (surface-mismatch, schema-mismatch)
#   pair         the consumer-declared redundant pair — the deployment
#                the manifest actually declares, not just its
#                definition: ci/pair.py reads the standby wiring and
#                persistence fields out of deploy/manifest.json and
#                spawns dcs-plant-server plus two released
#                dcs-controller --driven --remote instances wired per
#                the manifest, the declared --state-file/--journal-file
#                paths under a runner-owned scratch directory. The
#                standby converges to tracking through the served
#                GET /role, scans driven through POST /scan keep the
#                peers' images identical, a receipted demote/promote
#                switches the roles, and the run continues bumplessly
#                with the adopted receipts and the durable journal
#                files' transition records intact; two passes produce
#                identical digests (pair-failed, pair-nondeterministic).
#                The stage's negotiation leg (ci/negotiation.py) then
#                proves the deployed pair degrades honestly on the
#                misconfiguration a customer writing their own
#                manifests can produce: a third released
#                dcs-controller launched --standby <active> on a
#                foreign document — the emitted model doctored inside
#                MODEL_VERSION to a fingerprint the pair does not
#                serve, without --revised — reports the named
#                non-converged negotiation state through GET /role for
#                the observation window, POST /promote against it
#                answers the named refusal, and the active's field
#                writes, receipts, and journal stay undisturbed; the
#                foreign peer tears down before later legs and a
#                control peer on the pair's own model converges and
#                promotes normally; two passes produce identical
#                digests (negotiation-failed,
#                negotiation-nondeterministic)
#                The stage's startup-claim ordering leg,
#                ci/startup_claim.py on the same declared deployment:
#                with the pair switched so the field owner holds the
#                plant's writer claim, a third released controller
#                launched against the same plant address on a doomed
#                --journal-file — a corrupt first record its startup
#                replay cannot read — must abort before any preemptive
#                claim: the incumbent keeps role, tick, field writes,
#                and receipts, foreign probes stay fenced under the
#                standing claim, the doomed peer's files prove the run
#                never survived the failed replay, and the pair
#                restores its launch roles once the process stops; two
#                passes produce identical digests
#                (startup-claim-failed, startup-claim-nondeterministic)
#                The stage's refusal half, ci/refusal.py on the same
#                declared deployment: a POST /promote on the freshly
#                launched standby — before its first transfer — answers
#                the named not_converged refusal with no field
#                hand-off, a receipted write against a declared
#                writable point submitted to the tracking standby's
#                monitor answers the named not_active rejection with
#                the point unchanged in the active's served snapshot
#                and no command-side journal entry on either peer, and
#                once tracking the same promote succeeds — the active's
#                field writes, receipts, and journal undisturbed
#                throughout; two passes produce identical digests
#                (refusal-failed, refusal-nondeterministic)
#                The stage's failure-handover leg, ci/handover.py on
#                the same declared deployment: with the pair settled
#                and the group holding a duty demand, a proven
#                duty-pump failure — the p101-run field channel faulted
#                through the plant protocol's declared inject_fault —
#                hands duty to the standby pump inside the declared
#                bound with staged reporting the survivor, the faulted
#                pump's fault/avail reporting the exclusion, and the
#                managed p101-fault alarm annunciating with journaled
#                point_changed evidence; the remaining pump's channel
#                then faults for the none_available/all_faulted
#                annunciation, each input restores its declared
#                recovery, and the pair's roles never move; two passes
#                produce identical digests (handover-failed,
#                handover-nondeterministic)
#                The stage's takeover leg, ci/takeover.py on the same
#                declared deployment: with the pair tracking and the
#                pump group holding a duty demand on pump 1, the
#                emitted model's declared per-pump mode seam is
#                exercised through the receipted path — p101-mode
#                cutting the delivered command off the group's cmd_1
#                with the auto-leg carriers reporting the manual
#                selection and the pump-group status reflecting the
#                exclusion, p101-hand running the pump on the operator
#                demand while the declared thermal/moisture guards
#                still gate it — the protection input driven through
#                the plant protocol asserting the proven fault and its
#                managed alarm — p101-oos asserting the maintenance
#                inhibit, and the restore returning the pump to group
#                control with the served journal carrying each
#                attributed transition in order; two passes produce
#                identical digests (takeover-failed,
#                takeover-nondeterministic)
#                The stage's force-carryover leg, ci/force_carryover.py
#                on the same declared deployment: with the pair
#                tracking, a receipted force_point on a declared
#                writable In point — the emitted model marks only
#                internal In points writable, so the leg's p101-hand is
#                the honest target — submitted through the active's
#                POST /command; the forces entry and the
#                Uncertain(Substituted) sample asserted on both peers'
#                snapshots, the demote/promote switch issued, and the
#                promoted peer asserted still carrying the force — the
#                forced value at substituted quality — across scans; a
#                receipted unforce on the new active settling applied,
#                emptying forces, and resuming the point's unforced
#                serve — for the internal target the held-value rule
#                leaves the force's last stamp re-stamped Good; the
#                pair restored to its declared roles; two passes
#                produce identical digests
#                (force-carryover-failed,
#                force-carryover-nondeterministic)
#                The stage's tune-carryover leg, ci/tune_carryover.py
#                on the same declared deployment: with the pair
#                tracking, a receipted set_parameter on a declared
#                writable configuration point — the emitted model's
#                exercise sequencer step_1_out, the honest sequence
#                parameter whose parked table's demand lands on the
#                out port's next sample while nothing downstream
#                consumes it — submitted through the active's
#                POST /command; the settled receipt recorded, the
#                tuned value asserted live on both peers' served
#                parameter reports and on the out port's bound point
#                the declared signal sources, the demote/promote
#                switch issued, and the promoted peer asserted still
#                carrying the tune — the parameter report and the
#                signal's reading — across scans, its served journal
#                ordering the promotion's role_changed entries after
#                the tune's command_settled; a further set_parameter
#                on the new active settling applied with a fresh
#                receipt, the pair restored to its declared roles;
#                two passes produce identical digests
#                (tune-carryover-failed,
#                tune-carryover-nondeterministic)
#                The stage's force-release leg, ci/force_release.py
#                on the same declared deployment: with the pair
#                tracking, a receipted force_point on a declared
#                writable In point through the active's POST /command
#                — asserting the substituted quality on both peers'
#                snapshots across scans and the journaled applied
#                settlement — then a receipted unforce_point asserted
#                at its apply tick: the forces set empty, the point's
#                live value resumed — for the internal target the
#                force's last stamp re-stamped Good — a restore write
#                proving the live path, the pair switched and its
#                launch roles restored with the released state riding
#                the checkpoint, and every transition journaled on
#                the field owner's durable record with the standby's
#                adopted log answering the same receipts; two passes
#                produce identical digests
#                (force-release-failed,
#                force-release-nondeterministic)
#                The stage's stale-checkpoint leg,
#                ci/stale_checkpoint.py on the same declared
#                deployment — the consumer-side pin for the
#                receipted-command contract's adoption rule (WW-ENG-003,
#                WW-LCM-001): with the pair settled and tracking, a
#                receipted force_point on the declared writable
#                internal In point through the active's receipted path
#                — asserting the applied settle and the
#                substituted-quality stamp — the documented switch
#                run with the promoted peer asserted still carrying the
#                force, a receipted unforce_point through the new
#                active asserting the applied settle, the emptied
#                forces entry, and the journaled release on both peers'
#                adopted records — then the tracking peer restarted
#                onto its declared --state-file/--journal-file so it
#                re-adopts, driving a staler-image adoption per the
#                reproduction shape, asserting the force does not
#                re-stand: the served forces stays empty, the point's
#                live value keeps serving unforced, the adopted receipt
#                log preserves the unforce settlement, and no phantom
#                force receipt appears on either peer's journal; the
#                pair's launch roles restored; two passes produce
#                identical digests (stale-checkpoint-failed,
#                stale-checkpoint-nondeterministic)
#                The stage's burst-order leg, ci/burst_order.py on the
#                same declared deployment: with the pair tracking and
#                the pump group holding a full demand, the emitted
#                alarm set's deterministic consequential cascade
#                (WW-ENG-003, WW-ALM-003, WW-ALM-004) is driven through
#                the plant protocol's unfenced surface — a quality
#                fault on level-primary so backup-active annunciates
#                first, the power-fail contact written so the station
#                permissives drop and the power alarm fires while the
#                undrawn level climbs, then both run contacts faulted
#                so none-available/all-faulted land last — every driven
#                alarm's alarm/unacknowledged asserted through the
#                active's monitor, the durable journal's ordered
#                point_changed record preserving the driven activation
#                order with no dropped or reordered entries, the
#                restores journaling the returns in order, and the
#                pair's roles unchanged; two passes produce identical
#                digests (burst-order-failed,
#                burst-order-nondeterministic)
#                The stage's peer-announce leg, ci/peer_announce.py
#                on the same declared deployment: with the pair
#                tracking — the standby's per-scan pulls announcing
#                its own monitor address on the field owner, the
#                source a demoted owner later follows — a foreign
#                GET /checkpoint?peer=<closed-port> naming an
#                address that is not the pulling connection's own
#                must still answer the checkpoint read while the
#                crafted announce is refused, and the
#                demote/promote switch must reconverge the demoted
#                peer tracking on its real successor rather than
#                stranding it unsynchronized on the planted address;
#                the pair's launch roles then restore; two passes
#                produce identical digests (peer-announce-failed,
#                peer-announce-nondeterministic)
#                The stage's command-availability leg,
#                ci/availability.py on the same declared deployment:
#                with the pair tracking, the active's GET /resources
#                command rows audited self-consistent — every
#                available: false row carrying a named refusal, every
#                available row none — every served-unavailable
#                bound-point-writable command submitted through the
#                active's POST /command settling a named rejection
#                rather than applied, the declared-bound probes'
#                receipts naming the same refusal the row served, a
#                served-available command settling applied into both
#                peers' adopted receipt log, the emitted model's
#                kind-declared advance exercised in both directions
#                where the tooling publishes verdicts — its standing
#                refusal carried verbatim through the settled
#                command_refused — and the tracking standby's
#                /resources reporting identical verdicts throughout;
#                two passes produce identical digests
#                (availability-failed, availability-nondeterministic)
#                The stage's automatic-failover leg, ci/failover.py on
#                the same declared deployment: the manifest's
#                failover_budget arms the standby's --auto-promote —
#                then the pair is converged, the field-owning
#                container stopped, and the surviving peer's driven
#                scans asserted through its served surface: GET /role
#                reports the miss run under the degraded sync state,
#                the self-promotion lands at the declared budget's
#                scan boundary, the plant's writer claim fences a
#                foreign attachment while the promoted peer's writes
#                land (a fencing probe through the run's
#                plant-protocol client), driven scans and receipted
#                commands continue uninterrupted, and the durable
#                journal records the transition distinguishably from
#                an operator-requested switch; a variant severing the
#                standby leaves the active's field writes undisturbed
#                and reports no failover; a measurement run on the
#                settled pair then walks decision 42's declared
#                measurement contract — the emitted failover-select's
#                primary/backup field points, out feeding the
#                threshold chain, backup_active feeding the managed
#                Bool alarm, the chain's on_bad_demand fallback —
#                degrading the primary through the plant protocol's
#                quality-fault surface so the backup serves with the
#                chain still controlling on it and the managed alarm
#                annunciating journaled point_changed evidence,
#                degrading the backup as well so the declared
#                all-sources-bad fallback drops demand to
#                on_bad_demand rather than control on bad data, and
#                restoring the backup then the primary so the
#                selection and the alarms return per their declared
#                lifecycle with the pair's roles unchanged; two
#                passes produce identical
#                digests (failover-failed, failover-nondeterministic)
#                The stage's staged-vs-field divergence leg,
#                ci/divergence.py on the same declared deployment: with
#                the pair settled and the standby tracking, the
#                standby's checkpoint pulls are withheld for an
#                observation window — the driven run making the
#                partition literal — while a field-side write lands
#                through the run's dedicated plant-protocol client (the
#                same connection the simulate stage's
#                inject_fault/clear_fault ops use), the client joining
#                the field's writer claim under the duty's recorded
#                owner token so the write lands on the carried p101-cmd
#                output. With the pull path resumed, the stale peer's
#                served GET /role must report standby under the
#                diverged sync state naming the perturbed output, its
#                journal must carry the divergence_detected record, and
#                POST /promote must answer the named not_converged
#                refusal carrying the diverged report — no field
#                hand-off, the active's writes, receipts, and journal
#                undisturbed, the duty's continued writes restoring the
#                field so the standby's next same-tick comparison
#                resolves the verdict; a control leg runs the identical
#                window without the field-side write — the standby
#                reconverges and the documented demote/promote switch
#                succeeds, the refusal naming the staged-vs-field
#                divergence rather than partition staleness; two
#                passes produce identical digests
#                (divergence-missed, divergence-nondeterministic)
#                The stage's standby-restart leg,
#                ci/standby_restart.py on the same declared
#                deployment: with the pair tracking and a receipted
#                command settled, the tracking standby's container is
#                stopped and relaunched onto its declared
#                --state-file/--journal-file — the field owner driven
#                through the downtime with its writes landing and a
#                second command settling applied — the resumed peer
#                reporting standby rather than claiming the field and
#                reconverging to tracking inside the leg's declared
#                window, its journal file carrying the restart
#                boundary ordered after run 1's entries with seq
#                order intact, the active's journal undisturbed, and
#                the pair still promoting afterward; two passes
#                produce identical digests (standby-restart-failed,
#                standby-restart-nondeterministic)
#                The stage's report leg, ci/report.py on the same
#                declared deployment: with the pair tracking, one
#                managed alarm is driven through its
#                annunciation/ack/return lifecycle — the level-primary
#                quality fault annunciating the failover's alarm, the
#                receipted ack write pairing the annunciation to its
#                attributed acknowledgment, the cleared instrument
#                returning it — then the released dcs-alarm-report
#                computes the declared AlarmReport metric set
#                (WW-ALM-004) over the field owner's served journal
#                and, with --journal-file, over its manifest-declared
#                durable journal file — the emitted model's whole
#                alarm set computed per instance, the driven
#                lifecycle's measured counts and response pair
#                asserted, and the durable file's report answering the
#                served report's metric set identically; the tool's
#                refusal modes — an unreachable monitor, an unreadable
#                journal file — exit nonzero naming the failure; two
#                passes produce identical digests
#                (report-failed, report-nondeterministic)
#                The stage's command-switch leg, ci/command_switch.py
#                on the same declared deployment: with the pair settled
#                and tracking, the exercise sequencer's kind-declared
#                `advance` is invoked through the released `dcs-ctl
#                invoke` consumer tooling before and after a receipted
#                demote/promote — each submission settling exactly once
#                with the applied receipt attributed to the serving peer
#                and exactly one `command_settled` journal entry, no
#                replay of the old peer's settlement — the emitted
#                `step_completed` records continuing in tick order on
#                the promoted peer with unchanged component attribution
#                and no pre-promotion re-emission, a further invoke
#                submitted immediately before the restore switch
#                settling exactly once on the new active (never lost,
#                never double-applied), and the pair's launch roles
#                restored; two passes produce identical digests
#                (command-switch-failed,
#                command-switch-nondeterministic)
#                The stage's demote-pending leg, ci/demote_pending.py
#                on the same declared deployment — the consumer-side
#                exercise of the demote-boundary pending-command
#                settlement contract (WW-ENG-003, WW-LCM-001): with
#                the pair settled and tracking, a receipted
#                write_value admitted on the field owner and left
#                pending while the documented demote lands inside its
#                window and the converged standby promotes — the
#                promote's final sync carrying the still-`Accepted`
#                admission — the demoted peer's first quiesced scan,
#                driven before the promoted peer's first field-owning
#                scan, asserted journaling no command_settled for the
#                admission, still holding its suspended receipt, and
#                still reading the baseline image — never a phantom
#                applied settle on the fenced image, never a vanished
#                pending entry — then the admission settling exactly
#                once: applied once per peer through the carry or the
#                named superseded rejection journaled on the demoted
#                peer alone, both peers' adopted receipt logs
#                identical, the served images agreeing, and each
#                manifest-declared durable journal file carrying the
#                same settle record; the pair's launch roles
#                restored; two passes produce identical digests
#                (demote-pending-failed,
#                demote-pending-nondeterministic)
#                The stage's demote-reconvergence leg,
#                ci/demote_reconvergence.py on the same declared
#                deployment — the consumer-side exercise of the
#                demote-follow tracking-source contract the
#                #616/#618/#619/#620 defect fixes settle
#                (WW-ENG-003, WW-LCM-001): each controller bound on
#                the manifest's declared 0.0.0.0 listen host — the
#                wildcard bind shape the defect family recorded
#                verbatim — the pair converged and the documented
#                demote/promote switch run in both directions, each
#                demoted peer reconverging tracking on the source its
#                successor's announced pulls resolved dialable —
#                never the wildcard, never its own address, never a
#                foreign endpoint — holding it across a driven pull
#                train with snapshots, adopted receipt logs, and
#                journaled role transitions consistent and no
#                restart-like journal boundary, the launch roles
#                restored; two passes produce identical digests
#                (demote-reconvergence-failed,
#                demote-reconvergence-nondeterministic)
#                The stage's managed-lifecycle leg,
#                ci/managed_lifecycle.py on the same declared
#                deployment: with the pair tracking, the emitted
#                model's whole managed-alarm surface exercises on the
#                customer-owned pair (WW-ENG-003, WW-ALM-001,
#                WW-ALM-002) — a field-held fault annunciating the
#                backup-active managed Bool alarm's
#                alarm/unacknowledged with the journaled point_changed
#                record; the receipted ack settling applied under the
#                leg's actor and clearing the latch; the shelvable
#                low-level alarm's writable shelve point reporting
#                shelved and auto-releasing at the declared
#                max_shelve_ticks while the request still stands, the
#                journaled edges measuring the bound; the
#                never-shelvable high-level alarm's bound-but-
#                unwritable shelve point answering the named
#                not_writable refusal — journaled as a settled
#                rejection, no state changed; the pump's oos point
#                driving the declared out_of_service/suppressed wiring
#                while the alarms the model wires without those inputs
#                report neither, a driven fault proving alarm still
#                reports process truth while suppression withholds the
#                latch, and the return to service evaluating the
#                standing condition as a fresh trip cleared by the
#                receipted ack — every transition settled with actor
#                attribution, the durable journal carrying the
#                lifecycle in order, the pair's roles and driven
#                inputs restored; two passes produce identical digests
#                (managed-lifecycle-failed,
#                managed-lifecycle-nondeterministic)
#                The stage's managed-carryover leg,
#                ci/managed_carryover.py on the same declared
#                deployment — the consumer-side proof that the managed
#                alarm kinds' checkpointed run state carries across a
#                takeover on the customer-owned pair (WW-ENG-003,
#                WW-ALM-002, WW-LCM-001): with the pair tracking, a
#                per-pump fault alarm put out of service through its
#                wired oos point and tripped suppressed so its alarm
#                reports process truth with the latch withheld, and
#                the shelvable low-level alarm shelved mid-run through
#                its writable journaled shelve point, the documented
#                demote/promote landing inside the declared
#                max_shelve_ticks bound — the promoted peer asserting
#                shelved still stands and releases at the tick the
#                continued countdown expires, never a bound restarted
#                at the switch, out_of_service and suppressed standing
#                with evaluation held, every written point carried,
#                and both durable journals' ordered records continuous
#                across the switch — then every driven input and the
#                pair's roles restored; two passes produce identical
#                digests (carry-failed, carry-nondeterministic)
#                The stage's staging leg, ci/staging.py on the same
#                declared deployment — the consumer-side proof that
#                the deployed pair stages and de-stages on level
#                through the emitted model's declared setpoint chain
#                (WW-ENG-003, WW-CTL-001, WW-CTL-002): both pumps held
#                out of service through receipted write_value on their
#                declared writable oos points so the declared inflow
#                raises the wet-well level unopposed, the active's
#                monitor asserting demand moves 0→1→2 only at the
#                chain's own declared start/lag_start crossings with
#                duty_call/lag_call reporting and the group's staged
#                count and motor commands held at zero, the high
#                crossing annunciating the managed high-level alarm
#                with the journaled evidence; the releases restoring
#                the driven inputs so the standing demand stages the
#                group — the duty pump first, the lag inside the
#                declared start_delay_ticks, each pump's cmd/run field
#                outputs proving the delivered start — then the staged
#                pumps drawing the level down through the declared
#                de-stage order, the lag's run releasing before the
#                duty's and the journaled transitions landing in the
#                same order down to the below-cutoff floor; the
#                receipted ack clearing the alarm's latch, every
#                driven input restored, the pair's roles unchanged,
#                and the durable journal audited for the ordered
#                record the served journal answers identically; two
#                passes produce identical digests
#                (staging-failed, staging-nondeterministic)
#                The stage's out-of-service leg, ci/oos.py on the
#                same declared deployment — the consumer-side proof
#                that a receipted maintenance inhibit on the duty
#                pump's declared writable journaled oos point
#                excludes it on the customer-owned pair (WW-ENG-003,
#                WW-OPS-001, WW-ALM-002): with the pair tracking at
#                an idle assigned-duty baseline — duty naming the
#                pump whose oos the leg drives — the attributed
#                write drops the in-service cone (oos-ok through
#                oos-ok-avail-in and oos-ok-guard-in), the aggregated
#                avail and its delivered copy, handing duty to the
#                sibling inside the declared wiring bound with staged
#                reporting the available count and the held pump's
#                command released for the whole of the sibling's
#                service; each managed per-pump alarm reports the
#                out_of_service/suppressed states its declared
#                lifecycle bindings select — the bound fault alarm,
#                the unbound thermal/moisture kinds and the sibling's
#                set untouched — while a mid-OOS run-contact fault
#                still asserts alarm as process truth with the
#                unacknowledged latch withheld; the false write
#                returns the pump to availability and re-annunciates
#                the outlasted trip on suppression's release, the
#                receipted ack settles the latch, and the next
#                completed cycle's declared rotation hands duty back;
#                every managed transition journaled as ordered
#                point_changed entries beside the attributed receipts
#                with the tick-domain ordering the declared bound
#                measures, and the pair's roles unmoved throughout;
#                two passes produce identical digests (oos-failed,
#                oos-nondeterministic)
#                The stage's power-fail interlock leg,
#                ci/power_trip.py on the same declared deployment:
#                with the pair settled and the group holding a full
#                duty demand, the station power-fail contact driven
#                through the plant protocol drops `power-ok` and both
#                pumps' availability aggregates — the motor commands
#                releasing while the chain's `demand` still stands,
#                `none-available` annunciating, and the managed
#                `power-fail` alarm's `alarm`/`unacknowledged`
#                asserting with journaled `point_changed` evidence;
#                a receipted `power-fail-ack` clears the latch while
#                the condition stands, and the released contact
#                returns the permissives and re-stages the demand
#                inside the declared `min_off_ticks`/`start_delay_ticks`
#                bounds with the field outputs moving only on the
#                driven scan sequence and the pair's roles unchanged;
#                two passes produce identical digests
#                (power-trip-failed, power-trip-nondeterministic)
#                The stage's monitor-starvation leg,
#                ci/monitor_starvation.py on the same declared
#                deployment: with the armed pair settled — the
#                standby's --auto-promote carrying the manifest's
#                failover_budget — the saturating set of
#                incomplete-body connections held against the field
#                owner's monitor leaves the serving lane answering
#                GET /role, /snapshot, and /checkpoint inside the
#                declared per-request bound on both peers, the
#                standby's per-scan checkpoint pulls landing through
#                a window one pull wider than the armed miss budget —
#                no promoting/active transition and no
#                field_claim_lost on either durable journal — and the
#                field's writer claim fencing foreign probes; closing
#                the set restores driven scans and a receipted command
#                with the pair's roles unchanged; two passes produce
#                identical digests (monitor-starvation-failed,
#                monitor-starvation-nondeterministic)
#                The stage's commissioning/handover record leg,
#                ci/commissioning.py on the same declared deployment —
#                the pre-pilot WW-LCM-002 drill materializing the
#                commissioning record
#                docs/releases/commissioning-record.md declares: with
#                the pair converged, the plant protocol's point census
#                audited against the emitted model's declared channel
#                set — every point's declared direction and the served
#                image's sample equal to a value the field presented
#                at the scan boundary (the I/O checkout record); the
#                declared measurement driven at marks across the
#                threshold chain's declared span with both peers
#                serving each mark identically, then the receipted
#                mode/hand path exercising the output loop through
#                the field's delivered command and returned run
#                feedback (the loop-check evidence); every managed
#                alarm instance's declared rationalization record
#                audited verbatim on both peers (the sign-off); the
#                documented demote/promote switch and restore (the
#                handover procedure); the model, dynamics, and
#                manifest documents digested, each peer's
#                checkpoint-stamped fingerprint held equal to the
#                manifest's, and each durable journal and state file
#                reported (the documentation turnover) — the
#                completeness audit refusing a record missing any
#                named artifact; two passes produce identical digests
#                (commissioning-failed,
#                commissioning-nondeterministic,
#                commissioning-unchecked)
#                The stage's journal-boundary leg,
#                ci/journal_boundary.py on the same declared
#                deployment — the consumer-side mirror of the
#                rig-side journal-flood finding (#623, landed fix;
#                WW-ENG-003, WW-LCM-001): the tracking standby
#                restarted onto its declared files twice around two
#                floods of receipted journal-producing commands past
#                the served journal's retained bound, so its served
#                GET /journal must still answer both lifetimes'
#                run_boundary entries pinned ahead of the retained
#                tail — the first flood's evicted marker recovered at
#                the second restart's replay, the second's pinned
#                live — with strict seq order, the evicted stretch
#                reading as the usual numbering gap, and the ?since=
#                cursor past the last boundary answering exactly the
#                retained tail; the durable file retaining every
#                marker in order with contiguous seqs, the field
#                owner's single-lifetime record audited the same
#                way, and the pair's roles unmoved; two passes
#                produce identical digests (journal-boundary-failed,
#                journal-boundary-nondeterministic)
#   consumers    the replaceable-consumer boundary: the simulate
#                stage's deterministic driven run replays under each
#                consumer schedule — no UI attached, normal polling, a
#                stalled reader, disconnect/reconnect churn, malformed
#                and flooded traffic within the declared limits, and a
#                UI process restart — producing identical output and
#                receipt digests across every schedule and across two
#                passes (consumer-interference, consumer-nondeterministic)
#   ctl          the shipped operator CLI against the same driven run:
#                dcs-ctl's reads answer the served contract — signals,
#                schema, snapshot, events, resources — its invoke
#                submits the sequencer's kind-declared advance/reset
#                through the bounded receipted path with actor
#                attribution, settling applied receipts visible through
#                receipts and the journal, and the refusal modes —
#                undeclared command, malformed argument, unreachable
#                monitor — exit nonzero naming the failure; two passes
#                produce identical digests (ctl-failed,
#                ctl-nondeterministic)
#   upgrade      the documented repin upgrade (README §7): this tree's
#                composition is materialized pinned at the recorded
#                release rev, repinned to a later compatible revision,
#                and re-emitted — the bytes must equal the checked-in
#                model/plant.json — and the full pipeline re-runs under
#                the repin; the named incompatible crossings are refused
#                (emit-divergent, pin-unresolvable, crossing-unrefused)
#
# Environment:
#
#   DCS_REMOTE   the git remote the release crates and tooling resolve
#                from (default: the published origin below). The
#                workspace-side proof substitutes a file:// stand-in and
#                rewrites this repository's Cargo.toml to match.
#   DCS_REV      the pinned revision (default: the release-line rev
#                this repository's manifest records — the v0.2.0
#                commit whose tooling serves the interface registry,
#                declared commands and their live availability
#                verdicts, and emitted events the surface stage
#                proves).
#   DCS_UPGRADE_REV
#                the later compatible revision the upgrade stage repins
#                to (default: $DCS_REV — a same-revision repin, still
#                proving the mechanics; the workspace-side proof
#                substitutes the checkout's HEAD).
#   DCS_UPGRADE  set to 0 to skip the upgrade stage — the stage's own
#                repinned re-run uses this internally.
#   DCS_TOOLS    a directory holding prebuilt `dcs-model`,
#                `dcs-controller`, `dcs-plant-server`, `dcs-plant-ctl`,
#                `dcs-ctl`, and `dcs-alarm-report` binaries. When
#                unset, the check installs them from $DCS_REMOTE at
#                $DCS_REV — the contract's `cargo install --git`
#                mechanism — into a scratch root.

set -euo pipefail
cd "$(dirname "$0")/.."

DCS_REMOTE="${DCS_REMOTE:-https://github.com/Jan-Kaspar1/dcs.git}"
DCS_REV="${DCS_REV:-c2b5694d9fd6f6168b85c1dfc2e1542b369b3a3f}"
DCS_UPGRADE_REV="${DCS_UPGRADE_REV:-$DCS_REV}"
DCS_TOOLS="${DCS_TOOLS:-}"
TOOLS=""
TOOLS_REV=""
UPGRADE_DIR=""
INSTALL_ROOTS=""
RIG_DIR=""
SCRATCH=""

fail() {
    echo "$1" >&2
    exit 1
}

cleanup() {
    if [ -n "$UPGRADE_DIR" ]; then rm -rf "$UPGRADE_DIR"; fi
    if [ -n "$RIG_DIR" ]; then rm -rf "$RIG_DIR"; fi
    if [ -n "$SCRATCH" ]; then rm -rf "$SCRATCH"; fi
    for dir in $INSTALL_ROOTS; do rm -rf "$dir"; done
}
trap cleanup EXIT

# Resolves the released tooling's binaries into $TOOLS: the caller's
# $DCS_TOOLS directory when set, else `cargo install --git $DCS_REMOTE
# --rev <$1>` into a tracked scratch root — the contract's install
# mechanism — cached by the revision already resolved.
ensure_tools() {
    if [ -n "$DCS_TOOLS" ]; then
        TOOLS="$DCS_TOOLS"
        return
    fi
    if [ "$TOOLS_REV" = "$1" ] && [ -n "$TOOLS" ]; then return; fi
    local dir
    dir="$(mktemp -d)"
    if ! cargo install --quiet --git "$DCS_REMOTE" --rev "$1" \
            dcs-model dcs-controller dcs-plant dcs-monitor dcs-sim-net \
            --root "$dir"; then
        rm -rf "$dir"
        return 1
    fi
    # The delivered binary set — the `dcs-monitor` package ships both
    # operator tools, `dcs-ctl` and `dcs-alarm-report`, and
    # `dcs-sim-net` ships the plant-side `dcs-plant-ctl`; a revision
    # whose tooling predates one fails the install rather than the
    # stage that invokes it.
    local bin
    for bin in dcs-model dcs-controller dcs-plant-server dcs-ctl \
            dcs-alarm-report dcs-plant-ctl; do
        if [ ! -x "$dir/bin/$bin" ]; then
            rm -rf "$dir"
            return 1
        fi
    done
    INSTALL_ROOTS="$INSTALL_ROOTS $dir"
    TOOLS="$dir/bin"
    TOOLS_REV="$1"
}

# Rewrites the release-crate specifiers in $UPGRADE_DIR/Cargo.toml: $1
# replaces each dependency's pin fragment — `rev = "<sha>"`,
# `tag = "<name>"`, optionally carrying a `version = "…"` requirement —
# while the remote stays exactly as this tree records it.
repin() {
    python3 - "$UPGRADE_DIR/Cargo.toml" "$DCS_REMOTE" "$1" <<'PY'
import re, sys
path, remote, spec = sys.argv[1], sys.argv[2], sys.argv[3]
toml = open(path).read()
for name in ("dcs-build", "dcs-model"):
    toml, count = re.subn(
        name + r' = \{[^}]+\}',
        lambda _: name + ' = { git = "' + remote + '", ' + spec + ' }',
        toml,
    )
    if count != 1:
        sys.exit(f"repin: expected one {name} dependency, rewrote {count}")
open(path, "w").write(toml)
PY
}

echo "== resolve =="
cargo fetch --locked 2>/dev/null || {
    # --locked is the fast path; a materialized copy whose Cargo.toml was
    # rewritten to a transport stand-in re-resolves once.
    cargo fetch || fail "pin-unresolvable: cargo fetch failed for the pinned release"
}

echo "== build =="
cargo build --quiet || {
    cargo build 2>&1 | sed 's/^/  /' >&2
    fail "surface-incompatible: the composition does not compile against the pinned release"
}
TARGET_DIR="$(cargo metadata --format-version 1 --no-deps \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"
BIN="$TARGET_DIR/debug/pump-station"

echo "== lockfile =="
python3 - <<'PY' || fail "path-dependency-leak: Cargo.lock records a non-git source for a release crate"
import re, sys
lock = open("Cargo.lock").read()
sections = re.findall(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "[^"]+"\nsource = "([^"]+)"', lock)
release = {name: source for name, source in sections if name.startswith("dcs-")}
missing = {"dcs-build", "dcs-core", "dcs-model"} - release.keys()
if missing:
    sys.exit(f"release crates missing from Cargo.lock: {sorted(missing)}")
for name, source in sections:
    if name.startswith("dcs-") and not source.startswith("git+"):
        sys.exit(f"{name} resolved from {source}")
PY
echo "  release crates resolve from git sources only"

echo "== emit =="
EMIT_1="$(mktemp)"; EMIT_2="$(mktemp)"; SCEN_1="$(mktemp)"; SCEN_2="$(mktemp)"
"$BIN" > "$EMIT_1"
"$BIN" > "$EMIT_2"
cmp -s "$EMIT_1" "$EMIT_2" \
    || fail "emit-nondeterministic: two emission runs produced different bytes"
cmp -s "$EMIT_1" model/plant.json \
    || fail "stale-artifact: a fresh emit differs from the checked-in model/plant.json"
"$BIN" --scenario > "$SCEN_1"
"$BIN" --scenario > "$SCEN_2"
cmp -s "$SCEN_1" "$SCEN_2" \
    || fail "emit-nondeterministic: two scenario emissions produced different bytes"
cmp -s "$SCEN_1" ci/scenario.json \
    || fail "stale-artifact: a fresh emit differs from the checked-in ci/scenario.json"
echo "  emit is byte-stable and matches the checked-in artifacts"

echo "== tooling =="
ensure_tools "$DCS_REV" \
    || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_REV failed"
"$TOOLS/dcs-model" validate model/plant.json \
    || fail "tooling-rejected: dcs-model validate refused the checked-in model"
LINT="$("$TOOLS/dcs-model" lint model/plant.json)" \
    || fail "tooling-rejected: dcs-model lint refused the checked-in model"
[[ "$LINT" == *"no findings"* ]] \
    || fail "tooling-rejected: dcs-model lint reports findings: $LINT"
"$TOOLS/dcs-controller" model/plant.json --check \
    || fail "tooling-rejected: dcs-controller --check refused the checked-in model"
"$TOOLS/dcs-plant-server" model/plant.json --check-dynamics model/dynamics.json \
    || fail "tooling-rejected: dcs-plant-server --check-dynamics refused the checked-in dynamics document"
echo "  validate, lint, --check, and --check-dynamics accept the checked-in documents"

# The release record's schema artifacts: `docs/releases/<tag>/` lives
# in the same repository the crate and tooling pins resolve from, so
# the record is fetched through the same mechanism — the pinned
# revision's git remote. The manifest's `dcs_release` names the record
# directory; the files are read out of the fetched commit's tree.
SCRATCH="$(mktemp -d)"
DCS_RELEASE="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["dcs_release"])')"
git -C "$SCRATCH" init -q -b main
git -C "$SCRATCH" fetch --depth 1 --quiet "$DCS_REMOTE" "$DCS_REV" \
    || fail "pin-unresolvable: the pinned rev $DCS_REV could not be fetched for the release record"
RECORD="$SCRATCH/record"
mkdir -p "$RECORD"
for artifact in block-interfaces.schema.json plant-model.schema.json; do
    git -C "$SCRATCH" show "FETCH_HEAD:docs/releases/$DCS_RELEASE/$artifact" \
        > "$RECORD/$artifact" \
        || fail "pin-unresolvable: the pinned rev serves no docs/releases/$DCS_RELEASE/$artifact"
done

# `dcs-model <subcommand>` emitted at the pinned rev must equal the
# recorded artifact byte-for-byte — the consumer's non-drift leg for
# the served-registry and plant-model schemas the contract records as
# fetchable release artifacts. A divergence reports schema-drift on
# stderr and returns 1.
schema_nondrift() {
    local emitted
    emitted="$(mktemp)"
    if ! "$TOOLS/dcs-model" "$1" > "$emitted"; then
        rm -f "$emitted"
        echo "tooling-rejected: dcs-model $1 failed at the pinned rev" >&2
        return 1
    fi
    if ! cmp -s "$emitted" "$2"; then
        rm -f "$emitted"
        echo "schema-drift: dcs-model $1 at the pinned rev does not emit the recorded $DCS_RELEASE artifact $3" >&2
        return 1
    fi
    rm -f "$emitted"
}
schema_nondrift interface-schema "$RECORD/block-interfaces.schema.json" block-interfaces.schema.json || exit 1
schema_nondrift schema "$RECORD/plant-model.schema.json" plant-model.schema.json || exit 1
echo "  schema and interface-schema emit the $DCS_RELEASE record's artifacts byte-identically"

# A drifted artifact must report the diagnostic — the same leg against
# a doctored copy, so the recorded file stays pristine.
DOCTORED_SCHEMA="$SCRATCH/block-interfaces.doctored.json"
python3 - "$RECORD/block-interfaces.schema.json" "$DOCTORED_SCHEMA" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["required"].remove("tick")
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if out="$(schema_nondrift interface-schema "$DOCTORED_SCHEMA" block-interfaces.schema.json 2>&1)"; then
    fail "schema-drift-unchecked: a drifted record artifact passed the interface-schema non-drift leg"
fi
[[ "$out" == *"schema-drift"* ]] \
    || fail "schema-drift-unchecked: a drifted record artifact did not report schema-drift: $out"
echo "  a drifted record artifact refused: schema-drift"

# One `dcs-model diff` leg: $3 is `no changes` — the documents must
# diff clean — or a field the diff listing must name on the element $4
# names. A violated expectation reports diff-mismatch on stderr and
# returns 1; a passing leg prints the listing as the run's evidence.
assert_diff() {
    local old="$1" new="$2" want="$3" element="${4:-}" out
    if ! out="$("$TOOLS/dcs-model" diff "$old" "$new" 2>&1)"; then
        echo "diff-mismatch: dcs-model diff $old $new refused the documents: $out" >&2
        return 1
    fi
    if [ "$want" = "no changes" ]; then
        if [ "$out" != "no changes" ]; then
            echo "diff-mismatch: dcs-model diff $old $new reports differences on identical documents: $out" >&2
            return 1
        fi
    elif ! [[ "$out" == *"$element"* && "$out" == *"$want"* ]]; then
        echo "diff-mismatch: dcs-model diff $old $new does not name $want on $element: $out" >&2
        return 1
    fi
    printf '%s\n' "$out" | sed 's/^/    /'
}

# The doctored compatible revision — the same in-place doctoring the
# upgrade stage applies for the incompatible crossing, but staying
# inside MODEL_VERSION so the diff loads both sides: the primary level
# measurement's declared description changes.
DIFF_REVISED="$SCRATCH/model-revised.json"
python3 - model/plant.json "$DIFF_REVISED" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
signal = next(s for s in document["signals"] if s["name"] == "level-primary")
signal["description"] = "Primary wet-well level measurement — revised"
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
echo "  diff over the doctored compatible revision:"
assert_diff model/plant.json "$DIFF_REVISED" "description:" "signal 10010" || exit 1
echo "  diff over the identical document:"
assert_diff model/plant.json model/plant.json "no changes" || exit 1

# A leg whose expectation fails must report the diagnostic — asserting
# differences on the identical document, asserting none on the doctored
# revision, and diffing a document outside MODEL_VERSION each report
# diff-mismatch.
if out="$(assert_diff model/plant.json model/plant.json "description:" "signal 10010" 2>&1)"; then
    fail "diff-mismatch-unchecked: asserting differences on the identical document passed"
fi
[[ "$out" == *"diff-mismatch"* ]] \
    || fail "diff-mismatch-unchecked: the leg did not report diff-mismatch: $out"
if out="$(assert_diff model/plant.json "$DIFF_REVISED" "no changes" 2>&1)"; then
    fail "diff-mismatch-unchecked: asserting no changes on the doctored revision passed"
fi
[[ "$out" == *"diff-mismatch"* ]] \
    || fail "diff-mismatch-unchecked: the leg did not report diff-mismatch: $out"
DIFF_INVALID="$SCRATCH/model-incompatible.json"
python3 - model/plant.json "$DIFF_INVALID" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["version"] += 1
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if out="$(assert_diff model/plant.json "$DIFF_INVALID" "description:" "signal 10010" 2>&1)"; then
    fail "diff-mismatch-unchecked: diffing a document outside MODEL_VERSION passed"
fi
[[ "$out" == *"diff-mismatch"* ]] \
    || fail "diff-mismatch-unchecked: the leg did not report diff-mismatch: $out"
echo "  failed diff expectations refused: diff-mismatch"

# summary and signal-index over the checked-in model, recorded to the
# run's evidence — the check transcript carries the outputs verbatim
# with their digests.
SUMMARY_OUT="$("$TOOLS/dcs-model" summary model/plant.json)" \
    || fail "tooling-rejected: dcs-model summary refused the checked-in model"
INDEX_OUT="$("$TOOLS/dcs-model" signal-index model/plant.json)" \
    || fail "tooling-rejected: dcs-model signal-index refused the checked-in model"
echo "  dcs-model summary (sha256 $(printf '%s\n' "$SUMMARY_OUT" | sha256sum | cut -d' ' -f1)):"
printf '%s\n' "$SUMMARY_OUT" | sed 's/^/    /'
echo "  dcs-model signal-index (sha256 $(printf '%s\n' "$INDEX_OUT" | sha256sum | cut -d' ' -f1)):"
printf '%s\n' "$INDEX_OUT" | sed 's/^/    /'

echo "== alarm-validation =="
# The rejection half of decision 70's alarm record — a leg beside the
# acceptance checks above: ci/alarm_validation.py audits the emitted
# document's managed-alarm record (each instance's rationalization
# prose and priority/class/response_ticks codes), doctors copies of it
# — one managed alarm's required_action removed, the same field
# emptied, another kind's priority removed — and requires the released
# `dcs-controller --check` to refuse each naming the missing element,
# never a silent load, then checks the driven run's served
# components/parameters sections report the same record. Contract
# violations report `alarm-validation: …` lines on stderr.
run_alarm_validation() {
    python3 ci/alarm_validation.py \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json
}
ALARM_1="$(run_alarm_validation)" \
    || fail "alarm-validation-failed: the alarm-validation leg refused"
ALARM_2="$(run_alarm_validation)" \
    || fail "alarm-validation-failed: the alarm-validation leg refused"
[ "$ALARM_1" = "$ALARM_2" ] \
    || fail "alarm-validation-nondeterministic: two alarm-validation passes produced different digests"
echo "  $ALARM_1"

# The leg's refusal assertions must themselves be proven — a run that
# writes the pristine document where each doctored copy belongs must
# report every acceptance, never pass a silent load.
if out="$(python3 ci/alarm_validation.py \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --tamper skip-doctoring 2>&1)"; then
    fail "alarm-validation-unchecked: a run with the doctoring skipped passed"
fi
[[ "$out" == *"alarm-validation:"* ]] \
    || fail "alarm-validation-unchecked: the skipped-doctoring run did not report alarm-validation: $out"
echo "  a skipped-doctoring run refused: alarm-validation"

echo "== fingerprint =="
EMITTED_FP="$("$BIN" --fingerprint)"
MANIFEST_FP="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json"))["model"]["fingerprint"])')"
[ "$EMITTED_FP" = "$MANIFEST_FP" ] \
    || fail "manifest-fingerprint-mismatch: emitted model fingerprints $EMITTED_FP but deploy/manifest.json records $MANIFEST_FP"
echo "  fingerprint $EMITTED_FP matches the manifest"

# The served-bytes half of the model authorization: the recorded
# fingerprint must name what the deployed pair actually serves, not
# just the checked-in artifact — ci/fingerprint.py launches the
# manifest-declared pair on the released tooling and pulls each peer's
# stamped model digest through GET /checkpoint, reporting the named
# manifest-fingerprint-mismatch with the expected vs served fingerprint
# and the first diverging document section on any divergence. Two
# passes must produce identical digests.
run_fingerprint() {
    python3 ci/fingerprint.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_fingerprint)" \
    || fail "fingerprint-failed: the manifest-fingerprint leg did not hold — its evidence lines are above"
SECOND="$(run_fingerprint)" \
    || fail "fingerprint-failed: the manifest-fingerprint leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "fingerprint-nondeterministic: two fingerprint-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: the pair serving a model whose point ids were
# renumbered — identical component content — must surface the named
# diagnostic; a silent pass would leave the authorization unproven.
if out="$(run_fingerprint --tamper renumber-points 2>&1)"; then
    fail "fingerprint-unchecked: a served model with renumbered point ids passed the fingerprint leg"
fi
[[ "$out" == *"manifest-fingerprint-mismatch"* && "$out" == *"io_points"* ]] \
    || fail "fingerprint-unchecked: the renumber-points case did not report its named diagnostic: $out"
echo "  renumber-points: reported, manifest-fingerprint-mismatch"

# The dynamics half of the authorization, under the same canonical
# fingerprint contract: the optional `dynamics.fingerprint` must name
# the dynamics document the deployment serves — the manifest's
# declared `dynamics.path`, the read-only mount the rig instantiates
# and `dcs-plant-server --dynamics` merges — and the checked-in
# artifact must fingerprint it identically. A manifest omitting the
# optional field declares no pin; the served-vs-checked-in comparison
# still stands.
DYN_FP="$(python3 ci/dynamics_fingerprint.py --fingerprint model/dynamics.json)"
MANIFEST_DYN_FP="$(python3 -c 'import json; print(json.load(open("deploy/manifest.json")).get("dynamics", {}).get("fingerprint") or "")')"
if [ -n "$MANIFEST_DYN_FP" ]; then
    [ "$DYN_FP" = "$MANIFEST_DYN_FP" ] \
        || fail "manifest-fingerprint-mismatch: the checked-in dynamics fingerprints $DYN_FP but deploy/manifest.json records $MANIFEST_DYN_FP"
    echo "  dynamics fingerprint $DYN_FP matches the manifest"
else
    echo "  the manifest records no dynamics.fingerprint — the pin is undeclared"
fi

# The served-bytes half: ci/dynamics_fingerprint.py launches the
# manifest-declared pair on the released tooling serving the
# deployment's declared dynamics.path, fingerprints the document it
# actually runs, and holds it equal to the recorded fingerprint and
# the checked-in artifact — reporting manifest-fingerprint-mismatch
# with the expected vs served fingerprint and the first diverging
# element on any divergence. Two passes must produce identical
# digests.
run_dynamics_fingerprint() {
    python3 ci/dynamics_fingerprint.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_dynamics_fingerprint)" \
    || fail "dynamics-fingerprint-failed: the dynamics-fingerprint leg did not hold — its evidence lines are above"
SECOND="$(run_dynamics_fingerprint)" \
    || fail "dynamics-fingerprint-failed: the dynamics-fingerprint leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "dynamics-fingerprint-nondeterministic: two dynamics-fingerprint passes produced different digests"
echo "  $FIRST"

# The doctored case: the pair serving a dynamics document whose point
# references were renumbered — identical element content — must
# surface the named diagnostic; a silent pass would leave the
# authorization unproven.
if out="$(run_dynamics_fingerprint --tamper renumber-points 2>&1)"; then
    fail "dynamics-fingerprint-unchecked: a served dynamics document with renumbered points passed the fingerprint leg"
fi
[[ "$out" == *"manifest-fingerprint-mismatch"* && "$out" == *"element 0"* ]] \
    || fail "dynamics-fingerprint-unchecked: the renumber-points case did not report its named diagnostic: $out"
echo "  renumber-points: reported, manifest-fingerprint-mismatch"

echo "== deploy =="
# The rig-definition consistency check reports its own named
# diagnostics (rig-invalid, rig-unverifiable, rig-mismatch) on stderr.
python3 ci/deploy_rig.py

# The persistence fields' divergence cases, exercised against doctored
# scratch copies so the checked-in pair stays pristine: each must
# report rig-mismatch — a declared path missing its mount or flag, a
# flag or writable mount the manifest does not declare, a persistence
# mount left read-only — while the fields omitted outright (with their
# mounts and flags) stay a valid deployment. The same harness proves
# the optional topology section: a declared named pair validates —
# over the single pair and over a beyond-one-pair rig — while a
# member the rig does not declare, a member two pairs share, or a
# declared pair whose standby wiring does not close inside it each
# report rig-mismatch. The checked-in manifest carries no section —
# the single-pair default every passing case starts from.
RIG_DIR="$(mktemp -d)"
mkdir -p "$RIG_DIR/deploy" "$RIG_DIR/ci" "$RIG_DIR/model"
cp deploy/manifest.json deploy/compose.yaml "$RIG_DIR/deploy/"
cp ci/deploy_rig.py "$RIG_DIR/ci/"
cp model/plant.json model/dynamics.json "$RIG_DIR/model/"
cp "$RIG_DIR/deploy/compose.yaml" "$RIG_DIR/compose.pristine.yaml"
cp "$RIG_DIR/deploy/manifest.json" "$RIG_DIR/manifest.pristine.json"

rig_case() {
    cp "$RIG_DIR/compose.pristine.yaml" "$RIG_DIR/deploy/compose.yaml"
    cp "$RIG_DIR/manifest.pristine.json" "$RIG_DIR/deploy/manifest.json"
    python3 - "$RIG_DIR" "$1" <<'PY'
import json
import sys

root, case = sys.argv[1], sys.argv[2]
compose_path = root + "/deploy/compose.yaml"
manifest_path = root + "/deploy/manifest.json"
compose = open(compose_path).read()
manifest = open(manifest_path).read()
if case == "persistence-mount-divergence":
    # ctrl-a's writable volume moves off the declared paths.
    compose = compose.replace(
        "ctrl-a-data:/var/tmp", "ctrl-a-data:/srv/other", 1)
elif case == "persistence-flag-divergence":
    # ctrl-a's --journal-file argument diverges from the manifest.
    compose = compose.replace(
        "- /var/tmp/journal.jsonl", "- /var/tmp/other.jsonl", 1)
elif case == "persistence-mount-read-only":
    compose = compose.replace(
        "ctrl-a-data:/var/tmp", "ctrl-a-data:/var/tmp:ro", 1)
elif case == "undeclared-persistence-flag":
    # ctrl-a keeps its --journal-file while the manifest drops the
    # field — an undeclared flag.
    document = json.loads(manifest)
    del document["controllers"][0]["journal_file"]
    manifest = json.dumps(document, indent=2)
elif case == "undeclared-writable-mount":
    # ctrl-a gains writable storage the manifest declares nothing
    # under.
    compose = compose.replace(
        "      - ctrl-a-data:/var/tmp",
        "      - ctrl-a-data:/var/tmp\n      - ctrl-a-scratch:/scratch",
        1,
    )
    compose = compose.replace(
        "volumes:\n  ctrl-a-data:",
        "volumes:\n  ctrl-a-data:\n  ctrl-a-scratch:",
        1,
    )
elif case == "failover-flag-missing":
    # ctrl-b keeps its declared failover_budget while the rig
    # definition drops the --auto-promote flag.
    compose = compose.replace(
        "      - --auto-promote\n      - \"3\"\n", "", 1)
elif case == "failover-flag-undeclared":
    # The rig definition keeps --auto-promote while the manifest
    # drops the declaration — an undeclared flag.
    document = json.loads(manifest)
    del document["controllers"][1]["failover_budget"]
    manifest = json.dumps(document, indent=2)
elif case == "failover-wrong-peer":
    # The declared budget moves to the duty entry — automatic
    # failover arms a tracking standby only.
    document = json.loads(manifest)
    document["controllers"][0]["failover_budget"] = \
        document["controllers"][1].pop("failover_budget")
    manifest = json.dumps(document, indent=2)
elif case == "persistence-omitted":
    # Both fields omitted together with their flags and mounts — the
    # optional deployment a consumer without durable storage declares.
    document = json.loads(manifest)
    for controller in document["controllers"]:
        controller.pop("state_file", None)
        controller.pop("journal_file", None)
    manifest = json.dumps(document, indent=2)
    for line in (
        "      - ctrl-a-data:/var/tmp\n",
        "      - ctrl-b-data:/var/tmp\n",
        "      - --state-file\n      - /var/tmp/state.json\n",
        "      - --journal-file\n      - /var/tmp/journal.jsonl\n",
    ):
        compose = compose.replace(line, "")
elif case == "topology-declared":
    # The optional section naming the deployment's one pair —
    # additive vocabulary a single-pair manifest may carry.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-b"]}]
    }
    manifest = json.dumps(document, indent=2)
elif case == "topology-multi-pair":
    # Two named pairs over a four-controller rig — the
    # beyond-one-pair declaration the section exists for. The rig
    # grows the matching second pair's services and volumes, cloned
    # from the first pair's blocks on fresh names and ports.
    document = json.loads(manifest)
    document["controllers"] += [
        {
            "name": "ctrl-c",
            "listen": "0.0.0.0:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl",
        },
        {
            "name": "ctrl-d",
            "listen": "0.0.0.0:8083",
            "standby": "ctrl-c:8082",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl",
        },
    ]
    document["topology"] = {
        "pairs": [
            {"name": "station-a", "members": ["ctrl-a", "ctrl-b"]},
            {"name": "station-b", "members": ["ctrl-c", "ctrl-d"]},
        ]
    }
    manifest = json.dumps(document, indent=2)
    block_a = compose[
        compose.index("  ctrl-a:"):compose.index("  ctrl-b:")
    ]
    block_b = compose[compose.index("  ctrl-b:"):compose.index("\nnetworks:")]
    block_c = block_a.replace("ctrl-a", "ctrl-c").replace("8080", "8082")
    block_d = (
        block_b.replace("ctrl-b", "ctrl-d")
        .replace("ctrl-a:8080", "ctrl-c:8082")
        .replace("ctrl-a:", "ctrl-c:")
        .replace("8081", "8083")
        .replace('      - --auto-promote\n      - "3"\n', "")
    )
    compose = compose.replace(
        "\nnetworks:",
        "\n" + block_c + "\n" + block_d + "\nnetworks:",
        1,
    )
    compose = compose.replace(
        "  ctrl-a-data:\n  ctrl-b-data:\n",
        "  ctrl-a-data:\n  ctrl-b-data:\n  ctrl-c-data:\n  ctrl-d-data:\n",
        1,
    )
elif case == "topology-undeclared-member":
    # A named pair member the rig does not declare.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-z"]}]
    }
    manifest = json.dumps(document, indent=2)
elif case == "topology-shared-member":
    # Two named pairs claiming the same member.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [
            {"name": "station-a", "members": ["ctrl-a", "ctrl-b"]},
            {"name": "station-b", "members": ["ctrl-a", "ctrl-b"]},
        ]
    }
    manifest = json.dumps(document, indent=2)
elif case == "topology-unwired-pair":
    # The declared pair's wiring does not close inside it: dropping
    # the standby field and flag leaves two duty controllers the
    # section still calls a pair.
    document = json.loads(manifest)
    document["topology"] = {
        "pairs": [{"name": "station", "members": ["ctrl-a", "ctrl-b"]}]
    }
    del document["controllers"][1]["standby"]
    manifest = json.dumps(document, indent=2)
    compose = compose.replace(
        "      - --standby\n      - ctrl-a:8080\n", "", 1)
else:
    sys.exit("unknown rig case " + case)
open(compose_path, "w").write(compose)
open(manifest_path, "w").write(manifest)
PY
    local out
    if out="$(cd "$RIG_DIR" && python3 ci/deploy_rig.py 2>&1)"; then
        case "$1" in
            persistence-omitted|topology-declared|topology-multi-pair)
                echo "  $1: optional declaration — the manifest and the rig agree"
                return
                ;;
        esac
        fail "rig-mismatch-unchecked: the $1 divergence passed the rig check"
    fi
    case "$1" in
        persistence-omitted|topology-declared|topology-multi-pair)
            fail "rig-mismatch-unchecked: the $1 case reported: $out"
            ;;
    esac
    [[ "$out" == *"rig-mismatch"* ]] \
        || fail "rig-mismatch-unchecked: the $1 divergence did not report rig-mismatch: $out"
    echo "  $1 refused: rig-mismatch"
}

for divergence in persistence-mount-divergence persistence-flag-divergence \
        persistence-mount-read-only undeclared-persistence-flag \
        undeclared-writable-mount failover-flag-missing \
        failover-flag-undeclared failover-wrong-peer \
        persistence-omitted topology-declared topology-multi-pair \
        topology-undeclared-member topology-shared-member \
        topology-unwired-pair; do
    rig_case "$divergence"
done

echo "== simulate =="
run_simulation() {
    python3 ci/simulate.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json
}
FIRST="$(run_simulation)" || fail "scenario-failed: the scripted simulation's declared outcomes did not hold"
SECOND="$(run_simulation)" || fail "scenario-failed: the scripted simulation's declared outcomes did not hold"
[ "$FIRST" = "$SECOND" ] \
    || fail "scenario-nondeterministic: two simulation runs produced different digests"
echo "  $FIRST"

echo "== restart =="
# The lone-controller half of WW-LCM-001's restart-recovery evidence:
# the field-owning controller launches with the manifest-declared
# --state-file/--journal-file flags at runner-owned scratch paths,
# runs the deterministic scenario to a leg boundary — far enough to
# leave applied receipts and journaled transitions — is stopped, and
# relaunches onto the same files. The resumed run must continue at the
# persisted tick, its leg outcomes, receipts, and served field image
# equal to an uninterrupted reference pass, the journal's seq order
# continuing across the file's run-boundary marker. Two passes must
# produce identical digests.
run_restart() {
    python3 ci/restart.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_restart)" \
    || fail "restart-resume-failed: the restart leg did not hold — its evidence lines are above"
SECOND="$(run_restart)" \
    || fail "restart-resume-failed: the restart leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "restart-resume-nondeterministic: two restart-leg passes produced different digests"
echo "  $FIRST"

# The doctored cases: each tamper at the restart point must surface
# the named diagnostic — never a silently accepted tick-zero restart.
# A corrupt state file is refused by the relaunch's startup naming the
# file; a missing one cold-starts the resumed run at tick zero, which
# the leg's own checks catch and report.
for tamper in missing-state-file corrupt-state-file; do
    if out="$(run_restart --tamper "$tamper" 2>&1)"; then
        fail "restart-resume-unchecked: a $tamper passed the restart leg"
    fi
    case "$tamper" in
        missing-state-file) evidence="never reported a resume" ;;
        corrupt-state-file) evidence="does not hold a checkpoint" ;;
    esac
    [[ "$out" == *"$evidence"* ]] \
        || fail "restart-resume-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, restart-resume-failed"
done

echo "== surface =="
python3 ci/simulate.py \
    --surface \
    --schema-out "$SCRATCH/served-schema.json" \
    --plant-server "$TOOLS/dcs-plant-server" \
    --controller "$TOOLS/dcs-controller" \
    --model model/plant.json \
    --dynamics model/dynamics.json \
    --scenario ci/scenario.json \
    || fail "surface-mismatch: the served operator surface — signal index, page, descriptors, interface registry, declared commands, their availability verdicts, emitted events — does not match the emitted model's declared surface"

# The served GET /schema document against the fetched record artifact's
# declared structure — the consumer-side required-keys/field-shape
# conformance a non-Rust consumer runs (README §5 documents the
# boundary: full draft-2020-12 validation stays workspace-side).
# ci/schema_conformance.py reports schema-mismatch itself.
python3 ci/schema_conformance.py \
    --schema "$RECORD/block-interfaces.schema.json" \
    --document "$SCRATCH/served-schema.json"

# A served document missing or mistyping a required field must report
# the diagnostic — doctored copies, so the recorded document stays
# pristine.
served_case() {
    local doctored="$SCRATCH/served-$1.json" out
    python3 - "$SCRATCH/served-schema.json" "$doctored" "$1" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
case = sys.argv[3]
if case == "missing-required":
    del document["tick"]
elif case == "mistyped-required":
    document["tick"] = "not-a-tick"
elif case == "mistyped-nested":
    document["interfaces"][0]["interface"]["version"] = "1"
else:
    sys.exit("unknown served case " + case)
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
    if out="$(python3 ci/schema_conformance.py \
            --schema "$RECORD/block-interfaces.schema.json" \
            --document "$doctored" 2>&1)"; then
        fail "schema-mismatch-unchecked: the $1 case passed the served-schema conformance check"
    fi
    [[ "$out" == *"schema-mismatch"* ]] \
        || fail "schema-mismatch-unchecked: the $1 case did not report schema-mismatch: $out"
    echo "  $1 refused: schema-mismatch"
}
for case in missing-required mistyped-required mistyped-nested; do
    served_case "$case"
done

echo "== pair =="
# The redundant-pair half of WW-LCM-001's switchover evidence — the
# deployment the manifest declares, run: the deploy stage proves the
# wiring statically; this leg runs it. ci/pair.py reads the standby
# target and the persistence fields out of deploy/manifest.json, spawns
# dcs-plant-server plus the two declared controllers as released
# --driven --remote instances — the standby wired --standby at its
# named peer, each controller's declared --state-file/--journal-file
# at runner-owned scratch paths — converges the standby to tracking,
# drives scans through POST /scan on each peer, issues the receipted
# demote/promote switch, and asserts the run continues bumplessly with
# the adopted receipt log identical and each peer's durable journal
# file carrying the transition records. Two passes must produce
# identical digests.
run_pair() {
    python3 ci/pair.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_pair)" \
    || fail "pair-failed: the redundant-pair leg did not hold — its evidence lines are above"
SECOND="$(run_pair)" \
    || fail "pair-failed: the redundant-pair leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "pair-nondeterministic: two pair-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: a standby wired at a peer that never serves must
# surface the named diagnostic — never a silently unconverged pass.
if out="$(run_pair --tamper broken-peer-flag 2>&1)"; then
    fail "pair-unchecked: a broken peer flag passed the pair leg"
fi
[[ "$out" == *"never reported tracking"* ]] \
    || fail "pair-unchecked: the broken-peer-flag case did not report its named diagnostic: $out"
echo "  broken-peer-flag: reported, pair-failed"

# The negotiation leg: the checkpoint-negotiation refusal a
# misconfigured deployment earns — the consumer-side half of
# WW-LCM-001's named rejection of incompatible state. A third released
# controller launches --standby at the pair's field owner on a
# foreign-fingerprint document — the emitted model doctored inside
# MODEL_VERSION, launched without --revised, exactly the manifest
# mistake a customer writing their own manifests can produce. The peer
# must report standby plus the named degraded negotiation state
# through GET /role for the whole observation window — the pulled
# checkpoint's fingerprint named against its own — while the declared
# pair's images stay identical and the field owner's tick advances;
# POST /promote against it must answer the named refusal — 409
# not_converged carrying the degraded state, never a silent or wrong
# verdict — leaving the peer's reported state untouched. The active
# stays undisturbed throughout: the receipt log identical to the
# pre-attempt baseline and the journal's window additions carrying no
# role, fencing, divergence, restart, or command records. The foreign
# peer tears down before the declared pair's convergence is re-proven,
# and the control leg — the same third controller on the pair's own
# model — converges to tracking and promotes normally, proving the
# refusal names the negotiation failure rather than a rig defect. Two
# passes must produce identical digests.
run_negotiation() {
    python3 ci/negotiation.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_negotiation)" \
    || fail "negotiation-failed: the checkpoint-negotiation leg did not hold — its evidence lines are above"
SECOND="$(run_negotiation)" \
    || fail "negotiation-failed: the checkpoint-negotiation leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "negotiation-nondeterministic: two negotiation-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: requiring convergence on the foreign-fingerprint
# peer must surface the named diagnostic — the leg reporting the
# degraded negotiation state it actually saw, never a silent pass.
if out="$(run_negotiation --tamper expect-tracking 2>&1)"; then
    fail "negotiation-unchecked: an expect-tracking pass succeeded — the leg never noticed the wrong expectation"
fi
[[ "$out" == *"never reported tracking"* && "$out" == *"degraded"* ]] \
    || fail "negotiation-unchecked: the expect-tracking case did not report the degraded negotiation state it saw: $out"
echo "  expect-tracking: reported, negotiation-failed"

# The pair contract's startup-claim ordering leg, on the same
# manifest-declared deployment: ci/startup_claim.py settles the pair
# and issues the documented demote/promote switch so the field owner
# holds the plant's writer claim, then launches a third released
# controller against the same plant address whose startup inputs are
# doomed by construction — a corrupt first record in its declared
# --journal-file that the startup replay cannot read. The spawn must
# abort at startup validation naming the replay failure — never
# reporting a listener, never logging the preemptive claim — while
# the incumbent stays active, its tick advances, its writes and a
# mid-window receipted command keep landing, a foreign attachment's
# mutation probe stays fenced under the standing claim, and the
# incumbent's journal gains no disturbance records; the doomed peer's
# journal file must still hold exactly the corrupt record and its
# state file must never appear, and the pair restores its launch
# roles once the foreign process is gone. Two passes must produce
# identical digests.
run_startup_claim() {
    python3 ci/startup_claim.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_startup_claim)" \
    || fail "startup-claim-failed: the startup-claim ordering leg did not hold — its evidence lines are above"
SECOND="$(run_startup_claim)" \
    || fail "startup-claim-failed: the startup-claim ordering leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "startup-claim-nondeterministic: two startup-claim passes produced different digests"
echo "  $FIRST"

# The doctored case: a dead foreign claim stranded over the incumbent —
# the pre-fix defect's observable shape — must surface the named
# diagnostic rather than pass.
if out="$(run_startup_claim --tamper stranded-claim 2>&1)"; then
    fail "startup-claim-unchecked: a stranded foreign claim passed the startup-claim leg"
fi
[[ "$out" == *"disturbed the incumbent"* ]] \
    || fail "startup-claim-unchecked: the stranded-claim case did not report its named diagnostic: $out"
echo "  stranded-claim: reported, startup-claim-failed"

# The pair contract's refusal half, on the same manifest-declared
# deployment: ci/refusal.py catches the freshly launched standby before
# its first transfer — the documented induction — where POST /promote
# must answer the named not_converged refusal with no field hand-off;
# submits a receipted write against a declared writable point to the
# tracking standby's monitor, which must answer the named not_active
# rejection with the point unchanged in the active's served snapshot,
# the write absent from both peers' adopted receipt logs, and no
# command-side journal entry on either peer recording it as anything
# but the refusal; and promotes once tracking, where the same request
# succeeds — the active's field writes, receipts, and journal
# undisturbed throughout. Two passes must produce identical digests.
run_refusal() {
    python3 ci/refusal.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_refusal)" \
    || fail "refusal-failed: the role-gated refusal leg did not hold — its evidence lines are above"
SECOND="$(run_refusal)" \
    || fail "refusal-failed: the role-gated refusal leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "refusal-nondeterministic: two refusal-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg asserting the standby-directed write settles
# applied must surface the named diagnostic — never a silently
# unrefused pass.
if out="$(run_refusal --tamper expect-applied 2>&1)"; then
    fail "refusal-unchecked: a doctored write expectation passed the refusal leg"
fi
[[ "$out" == *"expected an applied receipt"* ]] \
    || fail "refusal-unchecked: the expect-applied case did not report its named diagnostic: $out"
echo "  expect-applied: reported, refusal-failed"

# The pair contract's failure-handover leg, on the same
# manifest-declared deployment: ci/handover.py settles the pair, waits
# for the group to hold a duty demand with pump 1 proven running,
# faults the duty pump's run-feedback field channel through the plant
# protocol's declared inject_fault, and asserts through the active's
# monitor that duty moves to the standby pump inside the declared
# bound, staged reports the survivor against the standing demand, the
# faulted pump's fault/avail report the exclusion, and the managed
# p101-fault alarm annunciates with journaled point_changed evidence;
# faults the remaining pump's channel asserting none_available and
# all_faulted annunciate with their managed alarms; and clears each
# injected fault asserting the declared recovery — fault flags clear,
# annunciation returns, the duty designation reassigns under the
# declared rotation, the unacknowledged latches hold — with the pair's
# controller roles unmoved throughout. Two passes must produce
# identical digests.
run_handover() {
    python3 ci/handover.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_handover)" \
    || fail "handover-failed: the duty-pump failure-handover leg did not hold — its evidence lines are above"
SECOND="$(run_handover)" \
    || fail "handover-failed: the duty-pump failure-handover leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "handover-nondeterministic: two handover-leg passes produced different digests"
echo "  $FIRST"

# The doctored cases: a leg expecting the failed pump to keep duty, or
# expecting none_available never to report, must surface the named
# diagnostic — never a silently wrong pass.
if out="$(run_handover --tamper keeps-duty 2>&1)"; then
    fail "handover-unchecked: a doctored duty expectation passed the handover leg"
fi
[[ "$out" == *"keep duty"* ]] \
    || fail "handover-unchecked: the keeps-duty case did not report its named diagnostic: $out"
echo "  keeps-duty: reported, handover-failed"

if out="$(run_handover --tamper none-available-silent 2>&1)"; then
    fail "handover-unchecked: a doctored none_available expectation passed the handover leg"
fi
[[ "$out" == *"never to report"* ]] \
    || fail "handover-unchecked: the none-available-silent case did not report its named diagnostic: $out"
echo "  none-available-silent: reported, handover-failed"

# The pair contract's manual-takeover leg, on the same
# manifest-declared deployment: ci/takeover.py converges the pair and
# drives the simulated well until the pump group holds a duty demand on
# pump 1, then exercises the emitted model's declared per-pump mode
# seam (WW-ENG-003, WW-OPS-001, WW-CTL-002) through the receipted path
# — p101-mode cutting the delivered command off the group's cmd_1 with
# the auto-leg carriers reporting the manual selection and the
# pump-group status handing the standing demand to pump 2; p101-hand
# running the pump on the operator demand while the declared
# thermal/moisture guards still gate it, the protection input driven
# through the plant protocol asserting the proven fault and its
# managed alarm; p101-oos asserting the maintenance inhibit — then
# restores the pump to group control, auditing the active's served
# journal for each attributed transition in order. Two passes must
# produce identical digests.
run_takeover() {
    python3 ci/takeover.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_takeover)" \
    || fail "takeover-failed: the manual-takeover leg did not hold — its evidence lines are above"
SECOND="$(run_takeover)" \
    || fail "takeover-failed: the manual-takeover leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "takeover-nondeterministic: two takeover-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg asserting the delivered command still
# follows the group while mode stands manual must surface the named
# diagnostic — never a silently unexercised pass.
if out="$(run_takeover --tamper follows-group 2>&1)"; then
    fail "takeover-unchecked: a doctored follows-group expectation passed the takeover leg"
fi
[[ "$out" == *"did not follow the group"* ]] \
    || fail "takeover-unchecked: the follows-group case did not report its named diagnostic: $out"
echo "  follows-group: reported, takeover-failed"

# The pair contract's force-carryover leg, on the same
# manifest-declared deployment: ci/force_carryover.py converges the
# pair, submits a receipted force_point on a declared writable In
# point through the active's POST /command — the emitted model marks
# only internal In points writable, so the leg's p101-hand is the
# honest target — asserts the snapshot's forces entry and the
# Uncertain(Substituted) sample on both peers while tracking, issues
# the demote/promote switch, and asserts the promoted peer still
# carries the force — the forced value at substituted quality —
# across scans. A receipted unforce on the new active must settle
# applied, empty the forces list, and resume the point's unforced
# serve — for the internal target the held-value rule leaves the
# force's last stamp re-stamped Good; the leg then restores the pair's
# declared roles. Two passes must produce identical digests.
run_force() {
    python3 ci/force_carryover.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_force)" \
    || fail "force-carryover-failed: the force-carryover leg did not hold — its evidence lines are above"
SECOND="$(run_force)" \
    || fail "force-carryover-failed: the force-carryover leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "force-carryover-nondeterministic: two force-carryover passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg asserting the unforced value after
# promotion must surface the named diagnostic — the force rides the
# checkpoint, never a silently released pass.
if out="$(run_force --tamper expect-unforced 2>&1)"; then
    fail "force-carryover-unchecked: a doctored unforced expectation passed the carryover leg"
fi
[[ "$out" == *"expected the unforced value"* ]] \
    || fail "force-carryover-unchecked: the expect-unforced case did not report its named diagnostic: $out"
echo "  expect-unforced: reported, force-carryover-failed"

# The pair contract's tune-carryover leg, on the same
# manifest-declared deployment: ci/tune_carryover.py converges the
# pair, submits a receipted set_parameter on a declared writable
# configuration point — the emitted model's exercise sequencer
# step_1_out, the honest sequence parameter whose out port feeds a
# declared signal — through the active's POST /command, records the
# settled receipt, issues the demote/promote switch, and asserts on
# the promoted peer's served surface that the tuned value is live:
# the snapshot's parameters report and the out point's
# declared-signal reading holding across driven scans, the promoted
# peer's served journal ordering the promotion's role_changed
# entries after the tune's command_settled, and a further
# set_parameter on the new active settling applied with a fresh
# receipt — the promoted peer's own command path live. The leg then
# restores the pair's declared roles. Two passes must produce
# identical digests.
run_tune() {
    python3 ci/tune_carryover.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_tune)" \
    || fail "tune-carryover-failed: the tune-carryover leg did not hold — its evidence lines are above"
SECOND="$(run_tune)" \
    || fail "tune-carryover-failed: the tune-carryover leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "tune-carryover-nondeterministic: two tune-carryover passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg asserting the parameter's original value
# after the tune must surface the named diagnostic — the tuned value
# rides the checkpoint, never a silently reverted pass.
if out="$(run_tune --tamper expect-original 2>&1)"; then
    fail "tune-carryover-unchecked: a doctored original-value expectation passed the carryover leg"
fi
[[ "$out" == *"expected the original value"* ]] \
    || fail "tune-carryover-unchecked: the expect-original case did not report its named diagnostic: $out"
echo "  expect-original: reported, tune-carryover-failed"

# The pair contract's force-release leg, on the same
# manifest-declared deployment: ci/force_release.py converges the
# pair, submits a receipted force_point on a declared writable In
# point through the active's POST /command — the emitted model marks
# only internal In points writable, so the leg's p101-hand is the
# honest target — asserting the Uncertain(Substituted) sample and the
# forces badge on both peers while the force stands across scans, and
# the journaled applied settlement. A receipted unforce_point must
# settle applied at the next scan boundary — the forces set emptying
# and the point resuming its unforced serve: for the internal target
# the held-value rule leaves the force's last stamp re-stamped Good,
# and the leg's restore write returns the pre-force held value. The
# pair then switches and restores its launch roles — the released
# state riding the checkpoint like any run state, never resurrecting
# a released force — and the field owner's durable journal file must
# carry each attributed transition in seq order with the standby's
# adopted log answering the same receipts. Two passes must produce
# identical digests.
run_force_release() {
    python3 ci/force_release.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_force_release)" \
    || fail "force-release-failed: the force-release leg did not hold — its evidence lines are above"
SECOND="$(run_force_release)" \
    || fail "force-release-failed: the force-release leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "force-release-nondeterministic: two force-release passes produced different digests"
echo "  $FIRST"

# The doctored cases: a release expectation left standing — the
# substitution asserted still badged after the unforce — and a record
# settling the release without its journaled transition must each
# surface the named diagnostic rather than pass silently.
for tamper in expect-standing unjournaled-release; do
    if out="$(run_force_release --tamper "$tamper" 2>&1)"; then
        fail "force-release-unchecked: a $tamper passed the release leg"
    fi
    case "$tamper" in
        expect-standing) evidence="expected the substitution still standing" ;;
        unjournaled-release) evidence="missing or out of order" ;;
    esac
    [[ "$out" == *"$evidence"* ]] \
        || fail "force-release-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, force-release-failed"
done

# The pair contract's stale-checkpoint leg, on the same
# manifest-declared deployment: ci/stale_checkpoint.py converges the
# pair, submits a receipted force_point on the declared writable
# internal In point through the active's POST /command — the emitted
# model marks only internal In points writable, so the leg's p101-hand
# is the honest target per the force-carryover convention — asserting
# the applied settlement and the Uncertain(Substituted) sample, issues
# the demote/promote switch asserting the promoted peer still carries
# the force, submits a receipted unforce_point on the new active
# asserting the applied settlement, the emptied forces set, and the
# journaled release on both peers' adopted records, then restarts the
# tracking peer onto its declared --state-file/--journal-file — the
# field owner driven through the downtime, the resumed peer
# reconverging to tracking inside the declared window — and asserts
# the re-adoption never re-stands the released force: the served
# forces stays empty, the point's live value keeps serving unforced,
# the adopted receipt log preserves the unforce settlement exactly
# once, and no phantom force receipt appears on either peer's served
# or durable journal. The leg then restores the pair's launch roles.
# Two passes must produce identical digests.
run_stale_checkpoint() {
    python3 ci/stale_checkpoint.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_stale_checkpoint)" \
    || fail "stale-checkpoint-failed: the stale-checkpoint leg did not hold — its evidence lines are above"
SECOND="$(run_stale_checkpoint)" \
    || fail "stale-checkpoint-failed: the stale-checkpoint leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "stale-checkpoint-nondeterministic: two stale-checkpoint passes produced different digests"
echo "  $FIRST"

# The doctored case: a re-adoption expectation left standing — the
# substitution asserted still badged after the tracker's restart —
# must surface the named diagnostic rather than pass silently.
if out="$(run_stale_checkpoint --tamper expect-standing 2>&1)"; then
    fail "stale-checkpoint-unchecked: an expect-standing passed the stale-checkpoint leg"
fi
[[ "$out" == *"expected the substitution still standing"* ]] \
    || fail "stale-checkpoint-unchecked: the expect-standing case did not report its named diagnostic: $out"
echo "  expect-standing: reported, stale-checkpoint-failed"

# The pair contract's alarm-burst leg, on the same manifest-declared
# deployment: ci/burst_order.py converges the pair and drives the
# simulated well until the pump group holds a full demand — both pumps
# staged and running — then drives the emitted alarm set's
# consequential cascade (WW-ENG-003, WW-ALM-003, WW-ALM-004) through
# the plant protocol's unfenced surface: a quality fault on
# level-primary so backup-active annunciates first, the power-fail
# contact written so the station permissives drop and the power alarm
# fires while the undrawn level climbs, then both run contacts'
# quality faulted so the proven motor faults roll up to all-faulted
# last. The leg asserts every driven alarm's alarm/unacknowledged
# through the active's monitor, audits the field owner's durable
# journal for the driven activations and their returns in seq order —
# the first-out record, with no dropped or reordered entries — and the
# pair's roles unchanged. Two passes must produce identical digests.
run_burst() {
    python3 ci/burst_order.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_burst)" \
    || fail "burst-order-failed: the alarm-burst leg did not hold — its evidence lines are above"
SECOND="$(run_burst)" \
    || fail "burst-order-failed: the alarm-burst leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "burst-order-nondeterministic: two burst-order passes produced different digests"
echo "  $FIRST"

# The doctored cases: a record missing a driven transition, or one
# carrying them out of order, must surface the named diagnostic —
# never a silently unexercised first-out proof.
for tamper in dropped-transition reordered-transition; do
    if out="$(run_burst --tamper "$tamper" 2>&1)"; then
        fail "burst-order-unchecked: a $tamper journal passed the burst leg"
    fi
    [[ "$out" == *"missing or out of order"* ]] \
        || fail "burst-order-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, burst-order-failed"
done

# The pair contract's peer-announce leg, on the same
# manifest-declared deployment: ci/peer_announce.py converges the
# pair — the standby's per-scan checkpoint pulls announcing its own
# monitor address on the field owner, the tracking source a demoted
# owner later follows — then issues a foreign GET
# /checkpoint?peer=<closed-port> naming an address that is not the
# pulling connection's own. The checkpoint read must still answer
# while the crafted announce is refused — it cannot overwrite the
# recorded tracking source. The demote/promote switch then proves
# the record: the crafted announce is issued again at the decisive
# point — after the promote's own re-announce, before the demoted
# peer's first tracking pull, the last write its fallback would
# follow — and the demoted peer reconverges tracking on its real
# successor rather than stranding unsynchronized on the planted
# address. The leg restores the pair's launch roles; two passes
# must produce identical digests.
run_peer_announce() {
    python3 ci/peer_announce.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_peer_announce)" \
    || fail "peer-announce-failed: the peer-announce leg did not hold — its evidence lines are above"
SECOND="$(run_peer_announce)" \
    || fail "peer-announce-failed: the peer-announce leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "peer-announce-nondeterministic: two peer-announce passes produced different digests"
echo "  $FIRST"

# The doctored case: a crafted announce naming the pulling
# connection's own source — a closed local port — lands exactly as it
# would on a controller whose acceptance check regressed. It joins
# the bounded announced set beside the genuine ones, and the
# demotion's per-candidate verification is what answers it: the
# planted hint names an endpoint no checkpoint pull can verify, so
# the adoption keeps the verified standby — the journal's
# tracking_source_adopted is the audit. The leg doctors its
# assertion to require the planted address, so a healthy demotion
# reports the adopted mismatch — a blind last-announcer adoption
# would satisfy the doctored expectation and pass silently.
if out="$(run_peer_announce --tamper landed-announce 2>&1)"; then
    fail "peer-announce-unchecked: a landed foreign announce passed the peer-announce leg"
fi
[[ "$out" == *"tracking_source_adopted"* ]] \
    || fail "peer-announce-unchecked: the landed-announce case did not report its named diagnostic: $out"
echo "  landed-announce: reported, peer-announce-failed"

# The pair contract's command-availability leg, on the same
# manifest-declared deployment: ci/availability.py converges the pair,
# then proves the per-command availability verdicts the active's
# GET /resources serves agree with what the receipted path settles —
# the consumer-facing honesty the served-interface contract owes
# (WW-ENG-003, WW-FND-003): every command row self-consistent — an
# available: false row carrying a named refusal, an available row none
# — every served-unavailable bound-point-writable command submitted
# through the active's POST /command settling a named rejection rather
# than applied, each declared-bound probe's receipt naming the same
# refusal the row served, and one served-available command settling
# applied identically into both peers' adopted receipt log. The
# emitted model's kind-declared advance is exercised in both
# directions where the tooling publishes verdicts — invocable
# mid-table, then the kind's named refusal carried verbatim through
# the settled command_refused once the table completes — and the
# tracking standby's /resources must report identical verdicts
# throughout: the same-adopted-state rule means availability never
# diverges across the pair. Two passes must produce identical digests.
run_availability() {
    python3 ci/availability.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_availability)" \
    || fail "availability-failed: the command-availability leg did not hold — its evidence lines are above"
SECOND="$(run_availability)" \
    || fail "availability-failed: the command-availability leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "availability-nondeterministic: two availability-leg passes produced different digests"
echo "  $FIRST"

# The doctored cases: a served-available command settling a refusal —
# the available probe submitted to the tracking standby's role gate —
# and a standby reporting different verdicts must each surface the
# named diagnostic rather than pass silently.
for tamper in refused-available diverged-standby; do
    if out="$(run_availability --tamper "$tamper" 2>&1)"; then
        fail "availability-unchecked: a $tamper passed the availability leg"
    fi
    case "$tamper" in
        refused-available) evidence="expected an accepted receipt" ;;
        diverged-standby) evidence="availability diverged across the pair" ;;
    esac
    [[ "$out" == *"$evidence"* ]] \
        || fail "availability-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, availability-failed"
done

# The pair contract's automatic-failover leg, on the same
# manifest-declared deployment: ci/failover.py arms the declared
# standby with the manifest's failover_budget — the deployment
# vocabulary's --auto-promote half — converges the pair, stops the
# field-owning container, and asserts through the surviving peer's
# monitor and the plant protocol: the served role path reports the
# miss run under the degraded sync state then active at the declared
# budget's scan boundary, the plant's writer claim fences a foreign
# attachment while the promoted peer's own writes land, subsequent
# driven scans and receipted commands continue uninterrupted, and
# the promoted peer's durable journal records the transition
# distinguishably from an operator-requested switch. A variant run
# severs the standby instead: the field owner's writes run
# undisturbed and nothing reports a failover. A measurement run on
# a freshly converged pair then exercises decision 42's declared
# measurement contract — the emitted model's failover-select,
# threshold chain, and managed backup-active alarm resolved from
# the artifact's wiring: the primary level source's quality fault
# asserts backup_active while the chain keeps controlling on the
# selected backup measurement and the managed alarm annunciates;
# the backup's fault as well engages the declared on_bad_demand
# fallback rather than control on bad data; restoring the backup
# then the primary returns the selection and the alarm per its
# declared lifecycle — the receipted ack clearing the standing
# latch — the durable journal carrying the transitions in driven
# order and the pair's roles unchanged. Two passes must produce
# identical digests.
run_failover() {
    python3 ci/failover.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_failover)" \
    || fail "failover-failed: the failover leg did not hold — its evidence lines are above"
SECOND="$(run_failover)" \
    || fail "failover-failed: the failover leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "failover-nondeterministic: two failover-leg passes produced different digests"
echo "  $FIRST"

# The doctored cases: legs expecting the standby promoted before the
# declared budget, the station still controlling on the bad primary,
# or a nonzero fallback demand must surface the named diagnostic —
# never a silently unexercised contract.
for tamper in early-promotion controls-on-bad nonzero-fallback; do
    if out="$(run_failover --tamper "$tamper" 2>&1)"; then
        fail "failover-unchecked: a doctored $tamper expectation passed the failover leg"
    fi
    case "$tamper" in
        early-promotion) evidence="expected the standby active at miss" ;;
        controls-on-bad) evidence="expected the station still controlling on the bad primary" ;;
        nonzero-fallback) evidence="expected the fallback demand nonzero" ;;
    esac
    [[ "$out" == *"$evidence"* ]] \
        || fail "failover-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, failover-failed"
done

# The pair contract's staged-vs-field divergence leg, on the same
# manifest-declared deployment: ci/divergence.py converges the pair,
# then withholds the tracking standby's checkpoint pulls for an
# observation window — the driven run making the partition literal —
# while a field-side write lands through the run's dedicated
# plant-protocol client (the connection the simulate stage's
# inject_fault/clear_fault ops use), the client joining the field's
# writer claim under the duty's recorded owner token so the write
# lands on the carried p101-cmd output the standby's staged image
# covers. With the pull path resumed, the stale peer's served GET
# /role must report standby under the diverged sync state naming the
# perturbed output, its served and durable journals must carry the
# divergence_detected record, and POST /promote must answer the named
# not_converged refusal carrying the diverged report — never a silent
# or wrong verdict and never a field hand-off of the stale image —
# while the active's writes, receipts, and journal run undisturbed,
# the duty's continued writes restoring the field so the standby's
# next same-tick comparison resolves the verdict. A control leg runs
# the identical window with no field-side write: the standby
# reconverges and the documented demote/promote switch succeeds —
# the refusal names the staged-vs-field divergence, not the
# partition's staleness. Two passes must produce identical digests.
run_divergence() {
    python3 ci/divergence.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_divergence)" \
    || fail "divergence-missed: the staged-vs-field divergence leg did not hold — its evidence lines are above"
SECOND="$(run_divergence)" \
    || fail "divergence-missed: the staged-vs-field divergence leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "divergence-nondeterministic: two divergence-leg passes produced different digests"
echo "  $FIRST"

# The doctored case: the field-side write skipped while the leg still
# asserts the diverged report and the refused promote must surface the
# named diagnostic — never a silently unconvinced pass.
if out="$(run_divergence --tamper skip-field-write 2>&1)"; then
    fail "divergence-unchecked: a skipped field-side write passed the divergence leg"
fi
[[ "$out" == *"expected the diverged report"* ]] \
    || fail "divergence-unchecked: the skip-field-write case did not report its named diagnostic: $out"
echo "  skip-field-write: reported, divergence-missed"

# The pair contract's standby-restart leg, on the same
# manifest-declared deployment: ci/standby_restart.py converges the
# pair and settles a receipted command into the adopted log, then
# stops the tracking standby's container and relaunches it onto its
# declared --state-file/--journal-file — the standby half of
# WW-LCM-001's restart-recovery clause on the deployed pair. The
# relaunch must report the resume at the persisted tick — never a
# silent cold start — rejoin in standby rather than claiming the
# field, and reconverge to tracking inside the leg's declared window
# while the field owner's driven scans keep writing and a second
# command settles applied. The standby's durable journal must carry
# the restart boundary ordered after run 1's entries with seq order
# intact, the active's journal runs undisturbed, and the documented
# switch must still promote the restarted peer — the restart left no
# wedge for later legs. Two passes must produce identical digests.
run_standby_restart() {
    python3 ci/standby_restart.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_standby_restart)" \
    || fail "standby-restart-failed: the standby-restart leg did not hold — its evidence lines are above"
SECOND="$(run_standby_restart)" \
    || fail "standby-restart-failed: the standby-restart leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "standby-restart-nondeterministic: two standby-restart passes produced different digests"
echo "  $FIRST"

# The doctored cases: a state file gone missing at the restart point,
# and the restart's assertions held against a peer never restarted,
# must each surface the named diagnostic — never a silently
# unrestarted pass.
for tamper in missing-state-file skip-restart; do
    if out="$(run_standby_restart --tamper "$tamper" 2>&1)"; then
        fail "standby-restart-unchecked: a $tamper passed the standby-restart leg"
    fi
    [[ "$out" == *"never reported a resume"* ]] \
        || fail "standby-restart-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, standby-restart-failed"
done

# The pair contract's alarm-report leg, on the same
# manifest-declared deployment: ci/report.py converges the pair, then
# drives one managed alarm through its lifecycle — the level-primary
# quality fault annunciating the failover's alarm, a receipted `ack`
# write through the active's POST /command pairing the annunciation to
# its attributed acknowledgment, the cleared instrument returning it —
# so the durable record carries one measured episode. The released
# dcs-alarm-report then computes the declared AlarmReport metric set
# (WW-ALM-004) twice: over the field owner's served journal, and over
# its manifest-declared durable journal file — the emitted model's
# whole alarm set computed per instance, the driven lifecycle's
# measured counts and response pair asserted, and the file's report
# answering the served report's metric set identically with only its
# run-boundary accounting added. The tool's refusal modes — an
# unreachable monitor and an unreadable journal file — must exit
# nonzero naming the failure. Two passes must produce identical
# digests.
run_report() {
    python3 ci/report.py \
        --alarm-report "$TOOLS/dcs-alarm-report" \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
[ -x "$TOOLS/dcs-alarm-report" ] \
    || fail "report-failed: the release tooling ships no dcs-alarm-report binary"
FIRST="$(run_report)" \
    || fail "report-failed: the alarm-report leg did not hold — its evidence lines are above"
SECOND="$(run_report)" \
    || fail "report-failed: the alarm-report leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "report-nondeterministic: two report-leg passes produced different digests"
echo "  $FIRST"

# The doctored cases: each tamper must surface the named diagnostic —
# a leg asserting the driven alarm left no activation must fail on the
# computed report's honest count, and the doctored invocations — a
# dead monitor address, a missing journal path — must fail the leg
# naming the refusal, never a silent pass.
for tamper in expect-quiet unreachable-monitor unreadable-journal; do
    if out="$(run_report --tamper "$tamper" 2>&1)"; then
        fail "report-unchecked: a $tamper case passed the report leg"
    fi
    case "$tamper" in
        expect-quiet) expected="expected zero activations" ;;
        unreachable-monitor) expected="unreachable monitor" ;;
        unreadable-journal) expected="unreadable journal file" ;;
    esac
    [[ "$out" == *"$expected"* ]] \
        || fail "report-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, report-failed"
done

# The pair contract's command-switch leg, on the same
# manifest-declared deployment: ci/command_switch.py converges the
# pair, holds the exercise sequencer's `run` so one `step_completed`
# emits, invokes the kind-declared `advance` through the released
# `dcs-ctl invoke` on the field owner — asserting the accepted
# submission settles applied with exactly one `command_settled`
# journal entry — switches, invokes the same declared command on the
# promoted peer with the same exactly-once attribution and no replay
# of the old peer's settlement, joins the emitted-event records
# continuing in tick order with unchanged attribution and no
# pre-promotion re-emission, carries a further invoke submitted
# immediately before the restore switch to exactly one applied
# settlement on the new active, and restores the launch roles. Two
# passes must produce identical digests.
run_command_switch() {
    python3 ci/command_switch.py \
        --ctl "$TOOLS/dcs-ctl" \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
[ -x "$TOOLS/dcs-ctl" ] \
    || fail "command-switch-failed: the release tooling ships no dcs-ctl binary"
FIRST="$(run_command_switch)" \
    || fail "command-switch-failed: the command-switch leg did not hold — its evidence lines are above"
SECOND="$(run_command_switch)" \
    || fail "command-switch-failed: the command-switch leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "command-switch-nondeterministic: two command-switch passes produced different digests"
echo "  $FIRST"

# The doctored cases: a submission settling zero times and one
# settling twice must each surface the named diagnostic — never a
# silently miscounted exactly-once proof.
for tamper in zero-settlement double-settlement; do
    if out="$(run_command_switch --tamper "$tamper" 2>&1)"; then
        fail "command-switch-unchecked: a $tamper case passed the command-switch leg"
    fi
    [[ "$out" == *"expected exactly one settlement"* ]] \
        || fail "command-switch-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, command-switch-failed"
done

# The pair contract's demote-boundary pending-command leg, on the
# same manifest-declared deployment: ci/demote_pending.py converges
# the pair, submits a receipted write_value on a declared writable
# internal In point through the field owner's POST /command and leaves
# it pending, then lands the documented demote on the owner and the
# promote on the converged standby inside that window — the demoted
# peer's first quiesced scan, driven before the promoted peer's first
# field-owning scan, audited for the suspended admission: no
# command_settled journaled on the fenced image, the accepted receipt
# still held, the baseline image unchanged — then the promoted peer
# settling the carried admission exactly once and both peers' served
# journals, adopted receipt logs, images, and durable journal files
# audited for the single audited settle, before the pair's launch
# roles restore. Two passes must produce identical digests.
run_demote_pending() {
    python3 ci/demote_pending.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_demote_pending)" \
    || fail "demote-pending-failed: the demote-pending leg did not hold — its evidence lines are above"
SECOND="$(run_demote_pending)" \
    || fail "demote-pending-failed: the demote-pending leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "demote-pending-nondeterministic: two demote-pending passes produced different digests"
echo "  $FIRST"

# The doctored cases: a leg expecting the phantom applied settle the
# fenced image must never journal, and one expecting the pending
# entry vanished from every receipt surface and journal — the
# unaudited-drop shape — must each surface the named diagnostic
# rather than passing silently.
for tamper in phantom-applied unaudited-drop; do
    if out="$(run_demote_pending --tamper "$tamper" 2>&1)"; then
        fail "demote-pending-unchecked: a $tamper case passed the demote-pending leg"
    fi
    case "$tamper" in
        phantom-applied) expected="phantom applied settle" ;;
        unaudited-drop) expected="unaudited drop" ;;
    esac
    [[ "$out" == *"$expected"* ]] \
        || fail "demote-pending-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, demote-pending-failed"
done

# The pair contract's demote-follow reconvergence leg, on the same
# manifest-declared deployment: ci/demote_reconvergence.py binds each
# controller's --listen on the manifest's declared 0.0.0.0 host — the
# wildcard bind shape the #616/#618/#619/#620 defect fixes settle —
# converges the pair, and runs the documented demote/promote switch
# in both directions: the launched active demotes onto the tracking
# source the standby's wildcard-announcing pulls recorded — resolved
# to the dialable peer address, never the wildcard, never the demoted
# peer's own address, never a foreign endpoint — reconverging
# tracking and holding it across a driven pull train, then the
# reverse switch restores the launch roles and the second demoted
# peer holds the same way. Served snapshots and adopted receipt logs
# stay identical throughout, each durable journal file carries its
# own role_changed transitions under the single cold-start boundary,
# and the declared persistence files hold the run's final tick. Two
# passes must produce identical digests.
run_demote_reconvergence() {
    python3 ci/demote_reconvergence.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_demote_reconvergence)" \
    || fail "demote-reconvergence-failed: the demote-reconvergence leg did not hold — its evidence lines are above"
SECOND="$(run_demote_reconvergence)" \
    || fail "demote-reconvergence-failed: the demote-reconvergence leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "demote-reconvergence-nondeterministic: two demote-reconvergence passes produced different digests"
echo "  $FIRST"

# The doctored case: a crafted ?peer= announce naming the field
# owner's own monitor address — a claim the pulling connection's own
# source proves, so it lands exactly as a self-claim — plants a
# self-addressed demotion hint beside the genuine announce: the
# self-pin defect shape this leg exists to catch. The demotion's
# verify pull on it reads the owner's own document — the replayable
# own-document shape a candidate can never satisfy — so the verified
# adoption keeps the genuine successor and the journal's
# tracking_source_adopted names it. The leg doctors its adoption
# audit to require the self-pin, so a healthy demotion reports the
# mismatch — a self-pinning regression would satisfy the doctored
# expectation and pass silently.
if out="$(run_demote_reconvergence --tamper self-announce 2>&1)"; then
    fail "demote-reconvergence-unchecked: a self-addressed announce passed the demote-reconvergence leg"
fi
[[ "$out" == *"tracking_source_adopted"* ]] \
    || fail "demote-reconvergence-unchecked: the self-announce case did not report its named diagnostic: $out"
echo "  self-announce: reported, demote-reconvergence-failed"

# The pair contract's managed-alarm lifecycle leg, on the same
# manifest-declared deployment: ci/managed_lifecycle.py converges the
# pair and exercises the emitted model's declared managed-alarm
# surface end to end — the field-driven activation asserting
# alarm/unacknowledged with the journaled record, the receipted ack
# clearing the latch under the leg's actor, the bounded shelve
# reporting shelved and auto-releasing at the declared
# max_shelve_ticks, the never-shelvable shelve write answering the
# named not_writable refusal with no state change, and the pump's oos
# driving the declared out_of_service/suppressed wiring through the
# suppressed trip and the return to service — then restores every
# driven input and audits the durable journal's ordered record. Two
# passes must produce identical digests.
run_managed_lifecycle() {
    python3 ci/managed_lifecycle.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_managed_lifecycle)" \
    || fail "managed-lifecycle-failed: the managed-alarm lifecycle leg did not hold — its evidence lines are above"
SECOND="$(run_managed_lifecycle)" \
    || fail "managed-lifecycle-failed: the managed-alarm lifecycle leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "managed-lifecycle-nondeterministic: two managed-lifecycle passes produced different digests"
echo "  $FIRST"

# The doctored cases: a leg asserting the never-shelvable shelve
# write settled applied — shelving landing where the model declares
# none — and a leg asserting the shelved flag still stands after the
# declared bound's auto-release must each surface the named
# diagnostic rather than passing silently.
for tamper in expect-applied expect-standing; do
    if out="$(run_managed_lifecycle --tamper "$tamper" 2>&1)"; then
        fail "managed-lifecycle-unchecked: a $tamper case passed the managed-lifecycle leg"
    fi
    case "$tamper" in
        expect-applied) expected="expected an applied receipt" ;;
        expect-standing) expected="still standing" ;;
    esac
    [[ "$out" == *"$expected"* ]] \
        || fail "managed-lifecycle-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, managed-lifecycle-failed"
done

# The pair contract's emit-identical event-parity leg, on the same
# manifest-declared deployment — decision 84's parity rule proven on
# the consumer's deployed pair: ci/event_parity.py converges the
# declared standby to tracking, drives the emitted model's sequencer
# through the field owner's receipted path — the `run` write refused
# `not_active` at the standby's role boundary — until the counted
# `step_completed` set stands, then asserts both peers' GET /resources
# views collect the same routed `event_emitted` records: identical
# outer and inner component attribution, declared identities, ordered
# fields, tick, and retention, the stream-local seqs excluded, while
# the standby still reports tracking. A UI consumer failing its event
# feed over between the peers must meet no gap in attribution. Two
# passes must produce identical digests.
run_event_parity() {
    python3 ci/event_parity.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_event_parity)" \
    || fail "event-parity-failed: the standby event-parity leg did not hold — its evidence lines are above"
SECOND="$(run_event_parity)" \
    || fail "event-parity-failed: the standby event-parity leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "event-parity-nondeterministic: two event-parity passes produced different digests"
echo "  $FIRST"

# The doctored cases: a standby whose served record set is missing an
# emission or carries one re-attributed must surface the named
# diagnostic — never a silently hollow or diverged parity pass.
for tamper in dropped-event-record reattributed-event-record; do
    if out="$(run_event_parity --tamper "$tamper" 2>&1)"; then
        fail "event-parity-unchecked: the $tamper case passed the parity leg"
    fi
    [[ "$out" == *"event-parity-failed"* ]] \
        || fail "event-parity-unchecked: the $tamper case did not report event-parity-failed: $out"
    echo "  $tamper: reported, event-parity-failed"
done

# The pair contract's monitor-starvation leg, on the same
# manifest-declared deployment: ci/monitor_starvation.py converges the
# armed pair — the standby's --auto-promote carrying the declared
# failover_budget — then holds the saturating set of incomplete-body
# connections against the field owner's monitor, the stalled-client
# shape that pinned every worker before the lane split (WW-ENG-003,
# WW-FND-004). Through the hold the serving lane must keep answering
# GET /role, /snapshot, and /checkpoint inside the declared
# per-request bound on both peers, the standby's per-scan checkpoint
# pulls must keep landing — the window running one driven pull past
# the armed budget, so a starved heartbeat produces the spurious
# self-promotion inside it — a foreign attachment's field probe must
# stay fenced, and neither durable journal may carry role_changed or
# field_claim_lost. Closing the set must free the submission lane: a
# driven POST /scan on the flooded owner answering again and a
# receipted kind-declared command settling applied into both peers'
# adopted log, the pair's roles unchanged. Two passes must produce
# identical digests.
run_starvation() {
    python3 ci/monitor_starvation.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_starvation)" \
    || fail "monitor-starvation-failed: the monitor-starvation leg did not hold — its evidence lines are above"
SECOND="$(run_starvation)" \
    || fail "monitor-starvation-failed: the monitor-starvation leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "monitor-starvation-nondeterministic: two monitor-starvation passes produced different digests"
echo "  $FIRST"

# The doctored cases: a leg whose liveness reads starve — the declared
# bound doctored to zero — and one whose armed standby reports a role
# change mid-hold must each surface the named diagnostic — never a
# silently unexercised contract.
for tamper in starved-reads peer-transition; do
    if out="$(run_starvation --tamper "$tamper" 2>&1)"; then
        fail "monitor-starvation-unchecked: a $tamper case passed the starvation leg"
    fi
    case "$tamper" in
        starved-reads) expected="never answered inside the declared" ;;
        peer-transition) expected="moved to role" ;;
    esac
    [[ "$out" == *"$expected"* ]] \
        || fail "monitor-starvation-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, monitor-starvation-failed"
done

# The pair contract's commissioning/handover record leg, on the same
# manifest-declared deployment — the pre-pilot WW-LCM-002 drill:
# ci/commissioning.py materializes the commissioning record
# docs/releases/commissioning-record.md declares from one
# deterministic driven run — the plant protocol's point census
# audited against the emitted model's declared channel set (the I/O
# checkout record), the declared measurement driven at marks across
# the threshold chain's span with both peers serving each mark
# identically and the receipted mode/hand path exercising the output
# loop through the field's delivered command and returned run
# feedback (the loop-check evidence), every managed alarm instance's
# declared record audited served verbatim on both peers (the alarm
# rationalization sign-off), the documented demote/promote switch
# and restore (the handover procedure), and the document set
# digested with each peer's checkpoint fingerprint, durable journal,
# and state file reported (the documentation turnover) — the
# completeness audit requiring every named artifact. Two passes must
# produce identical digests.
run_commissioning() {
    python3 ci/commissioning.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_commissioning)" \
    || fail "commissioning-failed: the commissioning leg did not hold — its evidence lines are above"
SECOND="$(run_commissioning)" \
    || fail "commissioning-failed: the commissioning leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "commissioning-nondeterministic: two commissioning passes produced different digests"
echo "  $FIRST"

# The doctored cases: a record assembled without one named artifact
# must fail the completeness audit naming it — never an incomplete
# record passing silently.
for tamper in missing-io-checkout missing-loop-check \
        missing-alarm-signoff missing-documentation-turnover; do
    if out="$(run_commissioning --tamper "$tamper" 2>&1)"; then
        fail "commissioning-unchecked: a $tamper record passed the commissioning leg"
    fi
    artifact="${tamper#missing-}"
    [[ "$out" == *"the record carries no $artifact artifact"* ]] \
        || fail "commissioning-unchecked: the $tamper case did not report its named diagnostic: $out"
    echo "  $tamper: reported, commissioning-failed"
done

# The pair contract's journal-boundary flood leg, on the same
# manifest-declared deployment: ci/journal_boundary.py converges the
# pair, then restarts the tracking standby onto its declared
# --state-file/--journal-file twice around two floods of
# journal-producing commands past the served journal's 1024-entry
# retained bound — the consumer-side mirror of the rig-side
# journal-flood finding (#623, landed fix; WW-ENG-003, WW-LCM-001).
# Each receipted write_value settles a `command_settled` on the field
# owner and an adopted one on the tracking peer, so both durable
# journals outgrow the bound: the standby's served GET /journal must
# still answer both lifetimes' run_boundary entries ahead of the
# retained tail — the first flood's eviction of run 2's marker
# recovered at the second restart's replay, the second flood's of
# run 3's pinned live — with strict seq order, the evicted stretch
# reading as the usual numbering gap, and the ?since= cursor past the
# last boundary answering exactly the retained tail; its durable file
# must retain every run_boundary marker in order with contiguous
# seqs; the field owner's single-lifetime journal stays bounded the
# same way with its cold-start marker retained and no served
# boundary by contract; and the pair's roles never move. Two passes
# must produce identical digests.
run_journal_boundary() {
    python3 ci/journal_boundary.py \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        --manifest deploy/manifest.json "$@"
}
FIRST="$(run_journal_boundary)" \
    || fail "journal-boundary-failed: the journal-boundary flood leg did not hold — its evidence lines are above"
SECOND="$(run_journal_boundary)" \
    || fail "journal-boundary-failed: the journal-boundary flood leg did not hold — its evidence lines are above"
[ "$FIRST" = "$SECOND" ] \
    || fail "journal-boundary-nondeterministic: two journal-boundary passes produced different digests"
echo "  $FIRST"

# The doctored case: a leg whose served tail loses every run_boundary
# while the durable file retains them must surface the named
# diagnostic — never a silently unattributed pass.
if out="$(run_journal_boundary --tamper dropped-boundaries 2>&1)"; then
    fail "journal-boundary-unchecked: a dropped-boundaries case passed the journal-boundary leg"
fi
[[ "$out" == *"journal-boundary-failed"* ]] \
    || fail "journal-boundary-unchecked: the dropped-boundaries case did not report journal-boundary-failed: $out"
echo "  dropped-boundaries: reported, journal-boundary-failed"

echo "== consumers =="
# The boundary lint half, alongside the lockfile stage's rule: the
# stage's driver and the README's consumer obligations name only
# released artifacts and documented endpoints — never a path into a
# platform checkout.
for file in ci/alarm_rationalization.py ci/alarm_validation.py \
        ci/availability.py \
        ci/burst_order.py ci/claim_fencing.py ci/command_switch.py \
        ci/commissioning.py ci/consumers.py \
        ci/ctl.py ci/demote_pending.py ci/deploy_rig.py \
        ci/divergence.py ci/failover.py \
        ci/force_carryover.py ci/force_release.py ci/handover.py \
        ci/journal_boundary.py \
        ci/managed_carryover.py ci/managed_lifecycle.py \
        ci/monitor_starvation.py \
        ci/negotiation.py ci/oos.py ci/pair.py \
        ci/peer_announce.py ci/power_trip.py \
        ci/refusal.py ci/report.py ci/restart.py \
        ci/schema_conformance.py ci/simulate.py ci/staging.py \
        ci/stale_checkpoint.py ci/standby_restart.py ci/startup_claim.py \
        ci/takeover.py ci/tune_carryover.py README.md; do
    if grep -nE 'crates/|\.\./|file://|/home/|target/debug' "$file"; then
        fail "path-dependency-leak: $file references a platform-checkout path"
    fi
done
# The behavioral half: the simulate stage's deterministic driven run
# replays once per consumer schedule. Identical digests across the
# schedules prove no consumer behavior — absent, polling, stalled,
# churning, malformed, or restarted — can change an output or a
# receipt; identical digests across two passes prove the stage itself
# is deterministic.
run_consumer_schedules() {
    local reference="" out digest
    for schedule in zero-clients polling stalled-reader \
            disconnect-reconnect malformed-and-flood ui-restart; do
        out="$(python3 ci/consumers.py \
            --plant-server "$TOOLS/dcs-plant-server" \
            --controller "$TOOLS/dcs-controller" \
            --model model/plant.json \
            --dynamics model/dynamics.json \
            --scenario ci/scenario.json \
            --schedule "$schedule")" \
            || fail "consumer-interference: the $schedule schedule did not hold — its evidence lines are above"
        digest="$(printf '%s\n' "$out" | sed -n 's/^consumer-digest //p')"
        [ -n "$digest" ] \
            || fail "consumer-interference: the $schedule schedule reported no digest"
        if [ -z "$reference" ]; then
            reference="$digest"
        elif [ "$digest" != "$reference" ]; then
            fail "consumer-interference: the $schedule schedule changed the run's outputs and receipts (digest $digest, reference $reference)"
        fi
        echo "  $schedule: consumer-digest $digest" >&2
    done
    echo "$reference"
}
FIRST="$(run_consumer_schedules)" || exit 1
SECOND="$(run_consumer_schedules)" || exit 1
[ "$FIRST" = "$SECOND" ] \
    || fail "consumer-nondeterministic: two consumer-stage passes produced different digests"
echo "  consumer-digest $FIRST identical across every schedule and both passes"

echo "== ctl =="
# The shipped operator CLI over the simulate stage's driven run: the
# leg drives scans through `dcs-ctl scan`, exercises the sequencer's
# kind-declared commands through `dcs-ctl invoke` and reads the served
# contract through the remaining subcommands — the release set's
# documented operator surface proven from the released binary alone.
run_ctl() {
    python3 ci/ctl.py \
        --ctl "$TOOLS/dcs-ctl" \
        --plant-server "$TOOLS/dcs-plant-server" \
        --controller "$TOOLS/dcs-controller" \
        --model model/plant.json \
        --dynamics model/dynamics.json \
        --scenario ci/scenario.json \
        || fail "ctl-failed: the dcs-ctl leg did not hold — its evidence lines are above"
}
FIRST="$(run_ctl)" || exit 1
SECOND="$(run_ctl)" || exit 1
[ "$FIRST" = "$SECOND" ] \
    || fail "ctl-nondeterministic: two ctl-stage passes produced different digests"
echo "  $FIRST identical across both passes"

if [ "${DCS_UPGRADE:-1}" != "0" ]; then

echo "== upgrade =="
# README §7's customer path exercised against this repository's own
# composition: materialize the tree pinned at the recorded release rev,
# repin to a later compatible revision, move the lockfile, and re-run
# the check — a same-minor repin is a drop-in upgrade, so the emitted
# bytes must not change (emit-divergent). The copy keeps the working
# tree untouched.
UPGRADE_DIR="$(mktemp -d)"
for path in Cargo.toml Cargo.lock rust-toolchain.toml README.md src model deploy ci; do
    cp -r "$path" "$UPGRADE_DIR/"
done
export CARGO_TARGET_DIR="$UPGRADE_DIR/target"

# The baseline: the composition as the recorded release rev emits it.
repin "rev = \"$DCS_REV\""
( cd "$UPGRADE_DIR" && cargo fetch ) \
    || fail "pin-unresolvable: the recorded release rev $DCS_REV did not resolve"
( cd "$UPGRADE_DIR" && cargo build --quiet ) \
    || fail "surface-incompatible: the composition does not compile against the recorded release rev"
UPGRADE_BIN="$UPGRADE_DIR/target/debug/pump-station"
"$UPGRADE_BIN" > "$UPGRADE_DIR/emit-released.json"
cmp -s "$UPGRADE_DIR/emit-released.json" model/plant.json \
    || fail "emit-divergent: the recorded release rev emits different bytes than the approved model/plant.json"

# The repin: only the pin changes — src/, deploy/, and model/ are the
# unchanged tree. The fetch re-resolves and moves the copied lockfile,
# README §7's `cargo update` step.
repin "rev = \"$DCS_UPGRADE_REV\""
( cd "$UPGRADE_DIR" && cargo fetch ) \
    || fail "pin-unresolvable: the repinned revision $DCS_UPGRADE_REV did not resolve"
( cd "$UPGRADE_DIR" && cargo build --quiet ) \
    || fail "surface-incompatible: the composition does not compile against the repinned revision"
"$UPGRADE_BIN" > "$UPGRADE_DIR/emit-upgraded.json"
cmp -s "$UPGRADE_DIR/emit-upgraded.json" model/plant.json \
    || fail "emit-divergent: the unchanged composition emitted different model bytes under $DCS_UPGRADE_REV"
echo "  byte-identical emit across the repin $DCS_REV -> $DCS_UPGRADE_REV"

# The full pipeline under the repin — this check's own stages re-run
# against the repinned materialization, with the release tooling
# resolved at the repinned revision.
ensure_tools "$DCS_UPGRADE_REV" \
    || fail "pin-unresolvable: cargo install --git $DCS_REMOTE --rev $DCS_UPGRADE_REV failed"
(
    cd "$UPGRADE_DIR"
    DCS_UPGRADE=0 DCS_REMOTE="$DCS_REMOTE" DCS_REV="$DCS_UPGRADE_REV" \
        DCS_TOOLS="$TOOLS" bash ci/check.sh
) || { echo "the repinned pipeline failed — its named diagnostic is above" >&2; exit 1; }
echo "  the full pipeline passes under the repin"

# The named incompatible crossings, against this tree: each must be
# refused — a crossing that resolves is the contract's
# crossing-unrefused.
expect_pin_refused() {
    repin "$1"
    local out
    if out="$( cd "$UPGRADE_DIR" && cargo fetch 2>&1 )"; then
        fail "crossing-unrefused: pin \`$1\` resolved — the incompatible crossing must be refused"
    fi
    echo "  $2 refused: pin-unresolvable"
    echo "$out" | tail -n 1 | sed 's/^/    /'
}
expect_pin_refused 'tag = "no-such-release"' "the unresolvable tag \`no-such-release\`"
expect_pin_refused "rev = \"$DCS_UPGRADE_REV\", version = \">=99\"" \
    "the pin outside the supported version window"

# A document outside MODEL_VERSION is refused by the released tooling.
DOCTORED="$UPGRADE_DIR/model-doctored.json"
python3 - model/plant.json "$DOCTORED" <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
document["version"] += 1
json.dump(document, open(sys.argv[2], "w"), indent=2)
PY
if "$TOOLS/dcs-model" validate "$DOCTORED" >/dev/null 2>&1; then
    fail "crossing-unrefused: dcs-model validate accepted a document outside MODEL_VERSION"
fi
echo "  a document outside MODEL_VERSION is refused"

fi
echo "check ok"
