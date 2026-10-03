"""Merge-flow classification shared by the rolling report and the planner input.

The bounded classifiers live here exactly once — the ``PARK_CAUSES`` classes,
the ``unclassified`` bucket for ledger rows without attributable detail, and
the concentrated/spread conflict-load rule — so ``scripts/merge_flow.py``'s
rolling report and ``State.merge_flow``'s planner summary can never disagree
on vocabulary or verdicts. All functions are read-only views over work-ledger
event dicts (``kind``/``issue``/``attempt``/``at``/``payload``) and job dicts.
"""
from collections import Counter
import json

from . import areas

# Bounded park-cause classes reported per window. The first seven name the
# known failure classes; "other" buckets everything else.
PARK_CAUSES = ("quota-kill", "timeout", "stall-kill", "ci-failure",
               "merge-conflict", "publish-error", "worker-failure", "other")

# Ledger event kinds that reserve a dispatch for an issue.
DISPATCH_KINDS = frozenset(("reserved", "retry-reserved"))

# Admission outcome categories ("invocation:<category>" rows) whose kill is a
# provider/quota event rather than a defect in the dispatched work.
QUOTA_INVOCATIONS = frozenset(("rate", "endpoint", "auth", "credits"))

# jobs.error substrings that identify a worker/invocation failure park.
_WORKER_FAILURE_MARKERS = ("local agent failed", "agent reported a blocker",
                         "missing process record", "recovery failed")

CHECK_RECORD_MARK = "as read-only diagnostics. "

# Ledger event kind recorded when a publish merge is completed in-process
# on mechanically resolvable paths — the no-repair counterpart of a
# 'repair' row with cause 'merge-conflict'. The separate kind is what lets
# the attribution separate mechanical resolutions from agent repairs.
MECHANICAL_RESOLUTION_KIND = "mechanical-resolution"

# Resolution classes a conflicted path is attributed to. ``resolved`` counts
# the paths a mechanical-resolution row resolved in the window;
# ``registered_unresolved`` counts merge-conflict-repair occurrences of paths
# the resolver table covers that resolved nothing then — the resolver refused
# the conflict's shape, or an unregistered path in the same all-or-nothing set
# kept it from running; ``unregistered`` counts occurrences on paths no
# resolver covers, the class that names where the next coverage decision
# belongs. A path may hold occurrences in two classes (the contract's keyed
# rows resolve on a pure append and are refused on the ``<leg>-unchecked``
# edit), which is the reading the coverage decision needs.
PATH_RESOLUTION_CLASSES = ("resolved", "registered_unresolved", "unregistered")


def registered_resolution_paths():
    """The conflicted paths carrying a registered mechanical resolver.

    Imported lazily because ``agent_pool.merge_resolution`` imports this
    module for the ledger kind; a module-level import back would be
    circular. The resolver table is the only registration record, so the
    report's classes and the supervisor's admission cannot drift.
    """
    from . import merge_resolution

    return frozenset(merge_resolution.MECHANICAL_RESOLVERS)


def window_bounds(now, window_seconds):
    """Named adjacent windows: previous=[now-2w, now-w), current=[now-w, now)."""
    boundary = now - window_seconds
    return {
        "previous": (boundary - window_seconds, boundary),
        "current": (boundary, now),
    }


def issue_area(issue):
    """Canonical managed-issue area (metadata, label, or legacy-group inference)."""
    try:
        return areas.issue_area(issue)
    except Exception:
        return None


def event_payload(event):
    """The decoded payload dict a ledger row carries, or {}."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def event_cause(event):
    """The bounded cause class an attributed ledger row carries, or None."""
    cause = event_payload(event).get("cause")
    return cause if isinstance(cause, str) and cause else None


def _payload_names(event, key):
    """A string-list detail field on a repair row's payload, or []."""
    value = event_payload(event).get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [name for name in value if isinstance(name, str) and name]


def job_check_names(job):
    """Failing check names a job's recorded ci-failure error exposes.

    The supervisor ends the ci-failure repair reason with the failed-check
    dict serialized as JSON; a blocked job keeps that record in jobs.error.
    Rows whose error carries no such record expose nothing — the caller
    never fabricates names.
    """
    error = (job or {}).get("error") or ""
    if CHECK_RECORD_MARK not in error:
        return []
    try:
        record = json.loads(error.rsplit(CHECK_RECORD_MARK, 1)[1].strip())
    except ValueError:
        return []
    if not isinstance(record, dict):
        return []
    return sorted(name for name in record if isinstance(name, str))


def _ranked(counter, **buckets):
    """Name->count dict ordered by incidence (count desc, name asc).

    Positive ``buckets`` entries append named coverage buckets after the
    ranked names for rows that carried no attributable detail —
    ``unclassified`` for rows without recorded detail, ``pathless`` for
    merge-conflict rows that recorded unrecoverable paths. They are
    coverage counts, not path or check names.
    """
    ranked = dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))
    for name, count in buckets.items():
        if count:
            ranked[name] = count
    return ranked


def _conflict_load(repairs, paths):
    """Whether a window's merge-conflict repairs concentrate on few paths."""
    attributed = sum(paths.values())
    if not repairs:
        return "none"
    if not attributed:
        return "unattributed"
    running = covering = 0
    for count in sorted(paths.values(), reverse=True):
        running += count
        covering += 1
        if running * 2 >= attributed:
            break
    return "concentrated" if covering <= 2 else "spread"


def _resolved_paths(event):
    """The paths one mechanical-resolution row resolved, from its resolver map."""
    resolvers = event_payload(event).get("resolvers")
    if not isinstance(resolvers, dict):
        return []
    return [path for path in resolvers if isinstance(path, str) and path]


def _path_resolution_classes(conflict_paths, resolved, registered):
    """Conflicted and resolved paths attributed per PATH_RESOLUTION_CLASSES.

    Every recorded occurrence lands in exactly one class: a
    mechanical-resolution row's paths as ``resolved``, and each
    merge-conflict repair's paths by whether the resolver table covers
    them — ``registered_unresolved`` when it does (that repair spent a
    bounded repair session on a path the resolver was admitted for and
    resolved nothing on), ``unregistered`` when it does not, which is
    the class that names the dominant path a coverage decision would
    have to earn.
    """
    classes = {name: Counter() for name in PATH_RESOLUTION_CLASSES}
    for path, count in conflict_paths.items():
        name = "registered_unresolved" if path in registered else "unregistered"
        classes[name][path] += count
    classes["resolved"].update(resolved)
    return {name: _ranked(counter) for name, counter in classes.items()}


def repair_attribution(events, lo, hi, issues_by_number, jobs_by_issue):
    """Repair/redispatch attribution for ledger rows in [lo, hi).

    Counts ``repair`` and ``redispatch`` rows by their bounded cause class,
    joins repairs to each issue's managed area, ranks the conflicted paths
    ``merge-conflict`` rows recorded (a ``pathless`` bucket counts rows
    that recorded unrecoverable paths, ``unclassified`` rows that predate
    recorded detail) with the concentrated/spread verdict, and ranks the
    failing check names ``ci-failure`` rows expose through their payload or
    the job's terminal check record. ``mechanical-resolution`` rows —
    publish merges completed in-process without a repair dispatch — are
    counted separately so a falling merge-conflict repair count can be
    read against them, and their resolved paths join the conflicted ones
    under ``conflict_paths_by_resolution`` (resolved /
    registered_unresolved / unregistered), so a path the resolver table
    covers but never resolved reads apart from one it does not cover.
    ``repairs_on_registered_paths`` counts the merge-conflict repairs whose
    whole recorded path set is registered: the ceiling on the repairs a
    mechanical resolution could have displaced in the window, read against
    ``mechanical_resolutions``.
    """
    repairs_by_cause = Counter()
    redispatches_by_cause = Counter()
    repairs_by_area = {}
    conflict_paths = Counter()
    conflict_repairs = conflict_pathless = conflict_unclassified = 0
    registered_only_repairs = 0
    resolved_paths = Counter()
    registered = registered_resolution_paths()
    mechanical_resolutions = 0
    failing_checks = Counter()
    checks_unattributed = 0
    for event in events or ():
        at = event.get("at")
        if not isinstance(at, (int, float)) or not lo <= at < hi:
            continue
        kind = event.get("kind")
        if kind == "redispatch":
            redispatches_by_cause[event_cause(event) or "unclassified"] += 1
            continue
        if kind == MECHANICAL_RESOLUTION_KIND:
            mechanical_resolutions += 1
            resolved_paths.update(_resolved_paths(event))
            continue
        if kind != "repair":
            continue
        cause = event_cause(event) or "unclassified"
        repairs_by_cause[cause] += 1
        issue = (issues_by_number or {}).get(event.get("issue"))
        area = (issue_area(issue) if issue else None) or "unresolved"
        repairs_by_area.setdefault(area, Counter())[cause] += 1
        if cause == "merge-conflict":
            conflict_repairs += 1
            paths = _payload_names(event, "paths")
            if paths:
                conflict_paths.update(paths)
                if all(path in registered for path in set(paths)):
                    registered_only_repairs += 1
            elif event_payload(event).get("pathless"):
                conflict_pathless += 1
            else:
                conflict_unclassified += 1
        elif cause == "ci-failure":
            names = _payload_names(event, "checks")
            if not names:
                names = job_check_names((jobs_by_issue or {}).get(event.get("issue")))
            if names:
                failing_checks.update(names)
            else:
                checks_unattributed += 1
    return {
        "repairs_by_cause": dict(sorted(repairs_by_cause.items())),
        "redispatches_by_cause": dict(sorted(redispatches_by_cause.items())),
        "repairs_by_area": {area: dict(sorted(causes.items()))
                            for area, causes in sorted(repairs_by_area.items())},
        "conflict_repairs": conflict_repairs,
        "mechanical_resolutions": mechanical_resolutions,
        "conflict_paths": _ranked(conflict_paths, pathless=conflict_pathless,
                                unclassified=conflict_unclassified),
        "conflict_load": _conflict_load(conflict_repairs, conflict_paths),
        "conflict_paths_by_resolution": _path_resolution_classes(
            conflict_paths, resolved_paths, registered),
        "repairs_on_registered_paths": registered_only_repairs,
        "failing_checks": _ranked(failing_checks, unclassified=checks_unattributed),
    }


def _resumes(event):
    """True when the ledger row moves its issue out of a blocked park."""
    kind = event.get("kind")
    return (kind in DISPATCH_KINDS or kind in ("redispatch", "repair",
                                              "merged-and-closed")
            or str(kind or "").startswith("status:"))


def _block_cause(park, history, job):
    """Classify one ``status:blocked`` transition into a bounded cause class.

    Evidence precedence: the recorded ``jobs.error`` applies only when the
    park is the issue's terminal ledger state and the job still sits blocked
    (a later resume rewrites ``error``); the quota and timeout classes also
    resolve for historical parks through the attempt's ``invocation:``
    outcome and the ``redispatch`` row that ended the park.
    """
    at = park.get("at") or 0
    before = [e for e in history if (e.get("at") or 0) <= at]
    after = [e for e in history if (e.get("at") or 0) > at]
    dispatched = [(e.get("at") or 0) for e in before if e.get("kind") in DISPATCH_KINDS]
    boundary = max(dispatched) if dispatched else None
    attempt = [e for e in before if boundary is None or (e.get("at") or 0) >= boundary]
    invocations = [e for e in attempt
                   if str(e.get("kind") or "").startswith("invocation:")]
    last_invocation = ((invocations[-1].get("kind") or "").split(":", 1)[1]
                       if invocations else None)
    repairs = [e for e in attempt if e.get("kind") == "repair"]
    terminal = not any(_resumes(e) for e in after)
    error = (job.get("error") if terminal and job
             and job.get("status") == "blocked" else None)
    if error:
        text = error.lower()
        if "repair limit exhausted" in text:
            if "merge conflict" in text:
                return "merge-conflict"
            if "ci failed" in text:
                return "ci-failure"
            if repairs:
                return event_cause(repairs[-1]) or "other"
            return "other"
        if "treating as hang" in text:
            return "stall-kill"
        if '"status": "timeout"' in text or '"status":"timeout"' in text:
            return "timeout"
    if last_invocation in QUOTA_INVOCATIONS:
        return "quota-kill"
    if last_invocation == "timeout":
        return "timeout"
    redispatch = next((e for e in after if e.get("kind") == "redispatch"), None)
    if redispatch is not None and event_cause(redispatch) == "quota-requeue":
        return "quota-kill"
    if last_invocation == "failure":
        return "worker-failure"
    if error:
        text = error.lower()
        if any(marker in text for marker in _WORKER_FAILURE_MARKERS):
            return "worker-failure"
        return "other"
    if len(repairs) >= 3:
        # The repair budget is consumed; the denied follow-on repair parked
        # the job and its cause repeats the last recorded repair class.
        return event_cause(repairs[-1]) or "other"
    return "other"


def _status_at(rows, hi):
    """Replay one issue's ledger rows to hi; return its status or None."""
    status = None
    for event in rows:
        at = event.get("at")
        if not isinstance(at, (int, float)) or at >= hi:
            continue
        kind = event.get("kind")
        if kind in DISPATCH_KINDS or kind in ("redispatch", "repair"):
            status = "working"
        elif kind == "merged-and-closed":
            status = "done"
        elif isinstance(kind, str) and kind.startswith("status:"):
            status = kind.split(":", 1)[1]
    return status


def flow_report(events, lo, hi, jobs_by_issue):
    """Per-window dispatch/park/merge attribution from the work ledger.

    ``dispatches`` counts dispatch reservations; ``parked`` counts
    ``status:blocked`` transitions with each park classified into
    PARK_CAUSES; ``merged_and_closed`` counts ledger completions;
    ``still_blocked`` replays the event stream to the window's end and adds
    jobs whose recorded blocked state predates it without ledger coverage.
    """
    events = events or []
    jobs_by_issue = jobs_by_issue or {}
    by_issue = {}
    for event in events:
        issue = event.get("issue")
        if isinstance(issue, int):
            by_issue.setdefault(issue, []).append(event)
    for rows in by_issue.values():
        rows.sort(key=lambda e: e.get("at") or 0)
    first = retry = parked = merged = 0
    causes = Counter()
    for event in events:
        at = event.get("at")
        if not isinstance(at, (int, float)) or not lo <= at < hi:
            continue
        kind = event.get("kind")
        if kind in DISPATCH_KINDS:
            if kind == "reserved":
                first += 1
            else:
                retry += 1
        elif kind == "status:blocked":
            parked += 1
            issue = event.get("issue")
            causes[_block_cause(event, by_issue.get(issue) or (),
                                jobs_by_issue.get(issue))] += 1
        elif kind == "merged-and-closed":
            merged += 1
    still_blocked = 0
    for issue, rows in by_issue.items():
        status = _status_at(rows, hi)
        if status == "blocked":
            still_blocked += 1
        elif status is None:
            job = jobs_by_issue.get(issue)
            if job and job.get("status") == "blocked" \
                    and (job.get("updated") or 0) < hi:
                still_blocked += 1
    for issue, job in jobs_by_issue.items():
        if issue not in by_issue and job.get("status") == "blocked" \
                and (job.get("updated") or 0) < hi:
            still_blocked += 1
    return {
        "dispatches": first + retry,
        "first_dispatches": first,
        "retry_dispatches": retry,
        "parked": parked,
        "merged_and_closed": merged,
        "still_blocked": still_blocked,
        "park_causes": {cause: causes.get(cause, 0) for cause in PARK_CAUSES},
    }
