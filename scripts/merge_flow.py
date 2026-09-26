#!/usr/bin/env python3
"""Read-only rolling merge-flow report for the delivery platform.

Computes, over a named adjacent window pair ("previous" and "current",
matching the supervisor's rolling merge comparison in ``State.merge_flow``):

- merge counts per window, from first-parent commits on the integration ref
  (each supervisor merge is one squash commit on ``main``);
- dispatch-to-merge lead-time percentiles where the merge's issue identity
  resolves (commit-message ``issue #N`` references) and the job ledger
  supplies a dispatch (first reservation) time;
- a per-area merge breakdown via the ``<!-- dcs-task:{...} -->`` managed-issue
  metadata (``area:`` label as its published mirror fallback);
- conflict-repair and re-dispatch incidence where the record exposes it:
  WIP-preservation and ``origin/main`` integration lines inside squash-merge
  bodies, plus the job ledger's ``repairs``/``attempt`` counters;
- repairs and redispatches broken down by bounded cause class, joined from
  the work ledger's attributed ``repair``/``redispatch`` event rows
  (``merge-conflict``/``ci-failure``/``publish-error`` repairs and
  ``worker-failure``/``quota-requeue`` redispatches), with repairs also
  joined to each issue's managed area;
- conflict attribution for decline analysis: the conflicted paths each
  ``merge-conflict`` repair's ledger row recorded (ranked by incidence, an
  ``unclassified`` bucket counting path-less rows) and a verdict naming
  whether the window's conflict load concentrates on few paths or spreads,
  plus the failing check names ``ci-failure`` repairs expose through their
  ledger row or the job's terminal check record;
- per-window flow attribution so a merge decline can be read as starvation,
  failure load, or exhausted supply: reserved dispatches (``reserved`` and
  ``retry-reserved`` rows), park-to-blocked transitions (``status:blocked``
  rows) classified into the bounded park-cause classes, ``merged-and-closed``
  completions, and jobs still blocked at each window's end reconstructed by
  replaying the ledger;
- the open backlog's ready/blocked label share at report time.

An issue's dependency state at a historical point is not reconstructible
from the ledger — the ledger records job transitions, not issue dependency
or label history — so per-window dependency-blocked supply is reported as a
limitation rather than fabricated; the report-time backlog section carries
the current split.

The report is deterministic for a fixed input set and ``--now``. All inputs
are injectable for tests: ``--git-log``/``--issues``/``--jobs``/``--events``
files replace the live git, GitHub, and SQLite reads. The script never
mutates anything.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_pool import areas  # noqa: E402 — repo package; canonical area classification

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
GIT_LOG_FORMAT = "%H%x1f%ct%x1f%s%x1f%b%x1e"

TASK_PREFIX = "<!-- dcs-task:"
ISSUE_REF = re.compile(r"(?:Implement issue|for issue|Closes)\s+#(\d+)")
PR_REF = re.compile(r"\(#(\d+)\)\s*$")
WIP_PRESERVE = "WIP: preserve interrupted work"
MAIN_INTEGRATION = re.compile(r"Merge remote-tracking branch 'origin/main'")

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

LIMITATIONS = (
    "park_causes classifies a job's terminal park from the recorded "
    "jobs.error; earlier parks of the same job classify from ledger context "
    "(redispatch class, invocation outcome, repair history) only, so "
    "historical stalls surface as worker-failure",
    "per-window dependency-blocked supply is not reconstructible: the work "
    "ledger records job transitions, not issue dependency or label history; "
    "the report-time backlog section carries the current split",
    "conflict-path attribution covers only repairs whose ledger row carries "
    "the conflicted paths git reported; older and unparseable rows count in "
    "the unclassified bucket, and a concentrated/spread verdict reads only "
    "the attributed share",
    "failing-check attribution covers ci-failure repairs whose ledger row "
    "or terminal jobs.error check record names the failed checks; other "
    "ci-failure repairs count in the unclassified bucket",
)

DEFAULT_STATE_DB = "~/.local/share/dcs-agents/state/state.sqlite3"
DEFAULT_CONFIG = "~/.config/dcs-agents/config.json"


def parse_task_metadata(body):
    """Return the managed-issue ``dcs-task`` metadata dict, or None."""
    body = body or ""
    if not body.startswith(TASK_PREFIX):
        return None
    try:
        end = body.index(" -->", len(TASK_PREFIX))
        value = json.loads(body[len(TASK_PREFIX):end])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def issue_area(issue):
    """Canonical managed-issue area (metadata, label, or legacy-group inference)."""
    try:
        return areas.issue_area(issue)
    except Exception:
        return None


def parse_git_log(text):
    """Parse ``git log --format=GIT_LOG_FORMAT`` output into commit records.

    Each record exposes the merge timestamp (committer date), the PR number
    when the subject carries the squash-merge ``(#N)`` suffix, the issue
    numbers referenced by the branch's commit messages, and the churn
    signals the squash body preserves: WIP-preservation commits (work
    re-dispatched after interruption) and ``origin/main`` integration
    commits (the branch had to absorb main mid-flight, including conflict
    repairs).
    """
    commits = []
    for record in text.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(FIELD_SEP)
        if len(fields) < 4:
            continue
        sha, timestamp, subject, body = fields[0], fields[1], fields[2], FIELD_SEP.join(fields[3:])
        # The squash body preserves the branch's own commit messages as
        # bullet lines; the subject is the merge title, not a work commit.
        body_lines = [line.strip() for line in body.splitlines()]
        issues = {int(m.group(1)) for line in [subject] + body_lines
                  for m in ISSUE_REF.finditer(line)}
        pr = PR_REF.search(subject)
        commits.append({
            "sha": sha,
            "timestamp": int(float(timestamp)),
            "subject": subject,
            "pr": int(pr.group(1)) if pr else None,
            "issues": sorted(issues),
            "wip_commits": sum(line.lstrip("* ").startswith(WIP_PRESERVE)
                               for line in body_lines),
            "main_integrations": sum(bool(MAIN_INTEGRATION.search(line))
                                     for line in body_lines),
            "work_commits": sum(bool(re.match(r"\*?\s*Implement issue #\d+", line))
                                for line in body_lines),
        })
    return commits


def merged_issue(commit):
    """The single issue a merge resolves to, or None when absent/ambiguous."""
    return commit["issues"][0] if len(commit["issues"]) == 1 else None


def percentile(sorted_values, fraction):
    """Linear-interpolated percentile on a sorted list; None when empty."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = fraction * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low)


def window_bounds(now, window_seconds):
    """Named adjacent windows: previous=[now-2w, now-w), current=[now-w, now)."""
    boundary = now - window_seconds
    return {
        "previous": (boundary - window_seconds, boundary),
        "current": (boundary, now),
    }


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


CHECK_RECORD_MARK = "as read-only diagnostics. "


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


def _ranked(counter, unclassified=0):
    """Name->count dict ordered by incidence (count desc, name asc).

    A positive ``unclassified`` appends the named bucket for rows that
    carried no attributable detail; it is a coverage count, not a path or
    check name.
    """
    ranked = dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))
    if unclassified:
        ranked["unclassified"] = unclassified
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


def window_report(commits, lo, hi, issues_by_number, jobs_by_issue, events=None):
    """Merge-flow measures for commits whose timestamp falls in [lo, hi)."""
    merges = [c for c in commits if lo <= c["timestamp"] < hi]
    areas = Counter()
    lead_hours = []
    resolved = unresolved = 0
    wip_merges = integration_merges = repeated = 0
    ledger_repair = ledger_redispatch = 0
    repairs_by_cause = Counter()
    redispatches_by_cause = Counter()
    repairs_by_area = {}
    conflict_paths = Counter()
    conflict_repairs = conflict_pathless = 0
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
        if kind != "repair":
            continue
        cause = event_cause(event) or "unclassified"
        repairs_by_cause[cause] += 1
        issue = issues_by_number.get(event.get("issue"))
        area = (issue_area(issue) if issue else None) or "unresolved"
        repairs_by_area.setdefault(area, Counter())[cause] += 1
        if cause == "merge-conflict":
            conflict_repairs += 1
            paths = _payload_names(event, "paths")
            if paths:
                conflict_paths.update(paths)
            else:
                conflict_pathless += 1
        elif cause == "ci-failure":
            names = _payload_names(event, "checks")
            if not names:
                names = job_check_names(jobs_by_issue.get(event.get("issue")))
            if names:
                failing_checks.update(names)
            else:
                checks_unattributed += 1
    for commit in merges:
        issue_number = merged_issue(commit)
        issue = issues_by_number.get(issue_number) if issue_number else None
        area = issue_area(issue) if issue else None
        areas[area or "unresolved"] += 1
        job = jobs_by_issue.get(issue_number) if issue_number else None
        if issue_number and job and job.get("started"):
            resolved += 1
            lead_hours.append((commit["timestamp"] - job["started"]) / 3600)
        else:
            unresolved += 1
        wip_merges += bool(commit["wip_commits"])
        integration_merges += bool(commit["main_integrations"])
        repeated += commit["work_commits"] > 1
        if job:
            ledger_repair += bool(job.get("repairs"))
            ledger_redispatch += (job.get("attempt") or 1) > 1
    lead_hours.sort()
    return {
        "merges": len(merges),
        "identity": {"resolved": resolved, "unresolved": unresolved},
        "areas": dict(sorted(areas.items())),
        "lead_time_hours": {
            "count": len(lead_hours),
            "p50": _round(percentile(lead_hours, 0.5)),
            "p90": _round(percentile(lead_hours, 0.9)),
            "max": _round(lead_hours[-1] if lead_hours else None),
        },
        "repair_incidence": {
            "merges_with_wip_preserve": wip_merges,
            "merges_with_main_integration": integration_merges,
            "wip_preserve_commits": sum(c["wip_commits"] for c in merges),
            "main_integration_commits": sum(c["main_integrations"] for c in merges),
            "merges_with_repeated_work_commits": repeated,
            "ledger_repairs": ledger_repair,
            "ledger_redispatched": ledger_redispatch,
            "repairs_by_cause": dict(sorted(repairs_by_cause.items())),
            "redispatches_by_cause": dict(sorted(redispatches_by_cause.items())),
            "repairs_by_area": {area: dict(sorted(causes.items()))
                                for area, causes in sorted(repairs_by_area.items())},
            "conflict_repairs": conflict_repairs,
            "conflict_paths": _ranked(conflict_paths, conflict_pathless),
            "conflict_load": _conflict_load(conflict_repairs, conflict_paths),
            "failing_checks": _ranked(failing_checks, checks_unattributed),
        },
        "flow": flow_report(events, lo, hi, jobs_by_issue),
    }


def _round(value):
    return round(value, 2) if value is not None else None


def backlog_report(issues):
    """Open-backlog label shares at report time.

    Ready issues are split by whether their managed dependencies are all
    closed — a ready queue that is mostly dependency-blocked is starvation,
    not supply.
    """
    open_issues = [i for i in issues if (i.get("state") or "").upper() == "OPEN"]
    closed = {i["number"] for i in issues if (i.get("state") or "").upper() == "CLOSED"}
    counts = Counter()
    ready_dependency_blocked = 0
    for issue in open_issues:
        labels = {row["name"] for row in issue.get("labels", [])}
        state = next((l[6:] for l in labels if l.startswith("agent:")), "unlabeled")
        counts[state] += 1
        if state == "ready":
            meta = parse_task_metadata(issue.get("body")) or {}
            if set(meta.get("dependencies") or []) - closed:
                ready_dependency_blocked += 1
    total = len(open_issues)
    return {
        "open": total,
        "managed": sum(1 for i in open_issues
                       if parse_task_metadata(i.get("body")) is not None),
        "by_label": dict(sorted(counts.items())),
        "ready_share": _round(counts["ready"] / total) if total else None,
        "blocked_share": _round(counts["blocked"] / total) if total else None,
        "ready_dependency_blocked": ready_dependency_blocked,
    }


def build_report(commits, issues, jobs, now, window_days, events=None):
    """Assemble the deterministic report dict from parsed inputs."""
    window_seconds = window_days * 86400
    bounds = window_bounds(now, window_seconds)
    issues_by_number = {i["number"]: i for i in issues or []}
    jobs_by_issue = {j["issue"]: j for j in jobs or []}
    windows = {
        name: dict(window_report(commits, lo, hi, issues_by_number, jobs_by_issue,
                                 events),
                   start=lo, end=hi)
        for name, (lo, hi) in bounds.items()
    }
    previous, current = windows["previous"]["merges"], windows["current"]["merges"]
    oldest = min((c["timestamp"] for c in commits), default=None)
    return {
        "window_days": window_days,
        "now": int(now),
        "windows": windows,
        "merge_decline_percent": _round(100 * (previous - current) / previous)
            if previous else None,
        "history": {
            "commits_scanned": len(commits),
            "oldest_commit": oldest,
            "covers_window_pair": bool(oldest is not None
                                       and oldest < bounds["previous"][0]),
        },
        "ledger": {
            "jobs": len(jobs_by_issue),
            "events": len(events or []),
            "ledger_merges": {
                name: sum(1 for j in jobs_by_issue.values()
                          if j.get("status") == "done" and j.get("updated")
                          and lo <= j["updated"] < hi)
                for name, (lo, hi) in bounds.items()
            },
        },
        "backlog": backlog_report(issues) if issues is not None else None,
        "limitations": list(LIMITATIONS),
    }


def _causes_text(causes):
    """Render a sorted cause-count dict compactly for the text report."""
    return " ".join("%s=%d" % (k, v) for k, v in causes.items()) or "none"


def render_text(report):
    """Deterministic plain-text rendering of the report dict."""
    lines = []
    lines.append("merge-flow report (window_days={window_days}, now={now})".format(**report))
    history = report["history"]
    coverage = "full" if history["covers_window_pair"] else "partial"
    lines.append("history: {commits_scanned} commits, coverage={coverage}".format(
        coverage=coverage, **history))
    for name in ("previous", "current"):
        w = report["windows"][name]
        lead = w["lead_time_hours"]
        repair = w["repair_incidence"]
        lines.append(
            "{name}: merges={merges} identity(resolved={ir} unresolved={iu}) "
            "lead_h(count={lc} p50={p50} p90={p90} max={mx}) "
            "repairs(wip={wip} main_integrations={mi} repeated_work={rw} "
            "ledger_repairs={lr} redispatched={rd}) "
            "repair_causes[{rc}] redispatch_causes[{dc}]".format(
                name=name, merges=w["merges"],
                ir=w["identity"]["resolved"], iu=w["identity"]["unresolved"],
                lc=lead["count"], p50=lead["p50"], p90=lead["p90"], mx=lead["max"],
                wip=repair["merges_with_wip_preserve"],
                mi=repair["merges_with_main_integration"],
                rw=repair["merges_with_repeated_work_commits"],
                lr=repair["ledger_repairs"], rd=repair["ledger_redispatched"],
                rc=_causes_text(repair["repairs_by_cause"]),
                dc=_causes_text(repair["redispatches_by_cause"])))
        flow = w["flow"]
        lines.append(
            "  flow: dispatches={d} (first={f} retry={r}) parked={p} "
            "merged_and_closed={m} still_blocked={sb}".format(
                d=flow["dispatches"], f=flow["first_dispatches"],
                r=flow["retry_dispatches"], p=flow["parked"],
                m=flow["merged_and_closed"], sb=flow["still_blocked"]))
        lines.append("  park_causes: " + _causes_text(flow["park_causes"]))
        lines.append("  areas: " + (", ".join(f"{k}={v}" for k, v in w["areas"].items()) or "none"))
        lines.append("  conflict_paths: " + _causes_text(repair["conflict_paths"])
                     + " load=" + repair["conflict_load"])
        lines.append("  failing_checks: " + _causes_text(repair["failing_checks"]))
        lines.append("  repairs_by_area: " + (
            "; ".join("%s[%s]" % (area, _causes_text(causes))
                      for area, causes in repair["repairs_by_area"].items())
            or "none"))
    decline = report["merge_decline_percent"]
    lines.append("merge decline: {}% (previous -> current)".format(decline))
    ledger = report["ledger"]
    if ledger["jobs"]:
        lm = ledger["ledger_merges"]
        lines.append("ledger: {jobs} jobs; ledger merges previous={p} current={c}".format(
            jobs=ledger["jobs"], p=lm["previous"], c=lm["current"]))
    backlog = report["backlog"]
    if backlog is None:
        lines.append("backlog: unavailable (no issue inventory)")
    else:
        lines.append("backlog: open={open} managed={managed} by_label={labels} "
                     "ready_share={rs} blocked_share={bs} "
                     "ready_dependency_blocked={rdb}".format(
                         open=backlog["open"], managed=backlog["managed"],
                         labels=backlog["by_label"], rs=backlog["ready_share"],
                         bs=backlog["blocked_share"],
                         rdb=backlog["ready_dependency_blocked"]))
    for limitation in report.get("limitations") or ():
        lines.append("limitation: " + limitation)
    return "\n".join(lines)


def read_git_log(repo_dir, ref):
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "log", ref, "--first-parent",
         "--format=" + GIT_LOG_FORMAT],
        capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("git log failed: " + result.stderr.strip())
    return result.stdout


def resolve_ref(repo_dir, ref):
    """Fall back to HEAD when the requested ref is unknown to this clone."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True)
    return ref if result.returncode == 0 and result.stdout.strip() else "HEAD"


def default_state_db():
    config = Path(DEFAULT_CONFIG).expanduser()
    if config.is_file():
        try:
            root = json.loads(config.read_text()).get("state_root")
            if root:
                candidate = Path(root) / "state.sqlite3"
                if candidate.is_file():
                    return candidate
        except (ValueError, OSError):
            pass
    candidate = Path(DEFAULT_STATE_DB).expanduser()
    return candidate if candidate.is_file() else None


def read_ledger(path):
    """Dispatch/repair ledger rows from the supervisor's durable SQLite state."""
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(
            "SELECT issue, started, updated, status, repairs, attempt, error FROM jobs")]
    finally:
        db.close()


def read_events(path):
    """Work-ledger event rows from the supervisor's durable SQLite state."""
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(
            "SELECT kind, issue, attempt, at, payload FROM work_events")]
    finally:
        db.close()


def fetch_issues(repo, repo_dir):
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "2000",
         "--json", "number,title,body,labels,state"],
        cwd=str(repo_dir), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("gh issue list failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def detect_repo(repo_dir):
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=str(repo_dir), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("gh repo view failed: " + result.stderr.strip())
    return result.stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=Path(__file__).resolve().parents[1],
                        type=Path, help="checkout to read git history from")
    parser.add_argument("--ref", default="origin/main",
                        help="integration ref to scan (falls back to HEAD)")
    parser.add_argument("--window-days", type=float, default=7.0)
    parser.add_argument("--now", type=float, default=None,
                        help="report time as epoch seconds (default: current time)")
    parser.add_argument("--git-log", type=Path,
                        help="read git-log output from FILE instead of running git")
    parser.add_argument("--issues", type=Path,
                        help="issue inventory JSON (gh issue list shape)")
    parser.add_argument("--repo", help="GitHub owner/name for gh issue list")
    parser.add_argument("--no-issues", action="store_true",
                        help="skip issue inventory; areas/backlog report unresolved")
    parser.add_argument("--jobs", type=Path,
                        help="job ledger JSON rows (issue, started, updated, status, repairs, attempt, error)")
    parser.add_argument("--events", type=Path,
                        help="work-event ledger JSON rows (kind, issue, attempt, at, payload)")
    parser.add_argument("--state-db", type=Path,
                        help="supervisor state.sqlite3; default: auto-detect install")
    parser.add_argument("--no-ledger", action="store_true",
                        help="skip the job and work-event ledgers; lead times report unresolved")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    now = args.now if args.now is not None else time.time()
    warnings = []

    if args.git_log:
        log_text = args.git_log.read_text()
        ref = args.ref
    else:
        ref = resolve_ref(args.repo_dir, args.ref)
        if ref != args.ref:
            warnings.append("ref %s unresolved; scanned %s" % (args.ref, ref))
        log_text = read_git_log(args.repo_dir, ref)
    commits = parse_git_log(log_text)

    issues = None
    if args.issues:
        issues = json.loads(args.issues.read_text())
    elif not args.no_issues:
        try:
            repo = args.repo or detect_repo(args.repo_dir)
            issues = fetch_issues(repo, args.repo_dir)
        except (RuntimeError, ValueError, OSError) as exc:
            warnings.append("issue inventory unavailable: %s" % exc)

    jobs = None
    events = None
    if args.jobs:
        jobs = json.loads(args.jobs.read_text())
    if args.events:
        events = json.loads(args.events.read_text())
    if not args.no_ledger and (jobs is None or events is None):
        state_db = args.state_db or default_state_db()
        if state_db:
            if jobs is None:
                try:
                    jobs = read_ledger(state_db)
                except (sqlite3.Error, OSError) as exc:
                    warnings.append("job ledger unavailable: %s" % exc)
            if events is None:
                try:
                    events = read_events(state_db)
                except (sqlite3.Error, OSError) as exc:
                    warnings.append("work-event ledger unavailable: %s" % exc)
        else:
            warnings.append("work ledger unavailable: no state db found")

    report = build_report(commits, issues, jobs, now, args.window_days, events)
    report["ref"] = ref
    if warnings:
        report["warnings"] = warnings
        for warning in warnings:
            print("merge_flow: " + warning, file=sys.stderr)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
