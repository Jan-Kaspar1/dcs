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
  ``worker-failure``/``quota-requeue`` redispatches);
- the open backlog's ready/blocked label share at report time.

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


def event_cause(event):
    """The bounded cause class an attributed ledger row carries, or None."""
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    cause = payload.get("cause") if isinstance(payload, dict) else None
    return cause if isinstance(cause, str) and cause else None


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
    for event in events or ():
        at = event.get("at")
        if not isinstance(at, (int, float)) or not lo <= at < hi:
            continue
        counter = {"repair": repairs_by_cause,
                   "redispatch": redispatches_by_cause}.get(event.get("kind"))
        if counter is not None:
            counter[event_cause(event) or "unclassified"] += 1
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
        },
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
        lines.append("  areas: " + (", ".join(f"{k}={v}" for k, v in w["areas"].items()) or "none"))
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
            "SELECT issue, started, updated, status, repairs, attempt FROM jobs")]
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
                        help="job ledger JSON rows (issue, started, updated, status, repairs, attempt)")
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
