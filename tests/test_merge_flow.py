"""Unit tests for scripts/merge_flow.py over synthetic fixtures."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import merge_flow

NOW = 1_800_000_000.0
DAY = 86400
WEEK = 7 * DAY


def git_log_text(*records):
    return "".join(
        "\x1f".join([sha, str(ts), subject, body]) + "\x1e\n"
        for sha, ts, subject, body in records)


def issue(number, area=None, labels=(), state="OPEN", deps=()):
    meta = {"key": f"task-{number}", "group": "crates/x", "dependencies": list(deps),
            "priority": 2}
    if area:
        meta["area"] = area
    body = "<!-- dcs-task:" + json.dumps(meta, separators=(",", ":")) + " -->"
    return {"number": number, "title": f"task {number}", "body": body, "state": state,
            "labels": [{"name": name} for name in labels]}


def job(number, started, updated, status="done", repairs=0, attempt=1):
    return {"issue": number, "started": started, "updated": updated,
            "status": status, "repairs": repairs, "attempt": attempt}


class ParseGitLogTests(unittest.TestCase):
    def test_parses_squash_merge_records(self):
        text = git_log_text(
            ("abc123", NOW, "Implement issue #42: add valve (#100)",
             "* Implement issue #42: add valve\n"),
            ("def456", NOW - 60, "[QA] flaky test (#99)",
             "* WIP: preserve interrupted work for issue #41\n"
             "* Implement issue #41: fix flaky test\n"
             "* Merge remote-tracking branch 'origin/main' into codex/issue-41-1\n"),
        )
        commits = merge_flow.parse_git_log(text)
        self.assertEqual(len(commits), 2)
        first, second = commits
        self.assertEqual(first["pr"], 100)
        self.assertEqual(first["issues"], [42])
        self.assertEqual(first["wip_commits"], 0)
        self.assertEqual(first["work_commits"], 1)
        self.assertEqual(second["pr"], 99)
        self.assertEqual(second["issues"], [41])
        self.assertEqual(second["wip_commits"], 1)
        self.assertEqual(second["main_integrations"], 1)

    def test_ambiguous_issue_references_do_not_resolve(self):
        text = git_log_text(
            ("abc", NOW, "Feature (#7)",
             "* Implement issue #1: a\n* Implement issue #2: b\n"),)
        commit = merge_flow.parse_git_log(text)[0]
        self.assertEqual(commit["issues"], [1, 2])
        self.assertIsNone(merge_flow.merged_issue(commit))

    def test_empty_log_parses_to_no_commits(self):
        self.assertEqual(merge_flow.parse_git_log(""), [])
        self.assertEqual(merge_flow.parse_git_log("\n"), [])


class PercentileTests(unittest.TestCase):
    def test_linear_interpolation(self):
        self.assertEqual(merge_flow.percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(merge_flow.percentile([1, 2, 3, 4, 5], 0.9), 4.6)
        self.assertEqual(merge_flow.percentile([7], 0.9), 7)
        self.assertIsNone(merge_flow.percentile([], 0.5))


class WindowReportTests(unittest.TestCase):
    def test_windowing_is_half_open(self):
        boundary = NOW - WEEK
        commits = merge_flow.parse_git_log(git_log_text(
            ("a", NOW - 1, "x (#1)", ""),            # current
            ("b", boundary, "x (#2)", ""),          # current (boundary inclusive)
            ("c", boundary - 1, "x (#3)", ""),      # previous
            ("d", boundary - WEEK, "x (#4)", ""),   # previous (lower bound inclusive)
            ("e", boundary - WEEK - 1, "x (#5)", ""),  # outside
        ))
        bounds = merge_flow.window_bounds(NOW, WEEK)
        cur = merge_flow.window_report(commits, *bounds["current"], {}, {})
        prev = merge_flow.window_report(commits, *bounds["previous"], {}, {})
        self.assertEqual(cur["merges"], 2)
        self.assertEqual(prev["merges"], 2)

    def test_per_area_and_lead_time(self):
        commits = merge_flow.parse_git_log(git_log_text(
            ("a", NOW - 10, "Implement issue #1: v (#10)", ""),
            ("b", NOW - 20, "Implement issue #2: w (#11)", ""),
            ("c", NOW - 30, "manual fix (#12)", ""),
        ))
        issues = {
            1: issue(1, area="operations", state="CLOSED"),
            2: issue(2, area="library", state="CLOSED"),
        }
        jobs = {
            1: job(1, NOW - 10 - 3600, NOW - 10),
            2: job(2, NOW - 20 - 7200, NOW - 20),
        }
        report = merge_flow.window_report(commits, NOW - WEEK, NOW, issues, jobs)
        self.assertEqual(report["merges"], 3)
        self.assertEqual(report["areas"], {"library": 1, "operations": 1, "unresolved": 1})
        self.assertEqual(report["identity"], {"resolved": 2, "unresolved": 1})
        lead = report["lead_time_hours"]
        self.assertEqual(lead["count"], 2)
        self.assertEqual(lead["p50"], 1.5)
        self.assertEqual(lead["max"], 2.0)

    def test_repair_incidence_from_history_and_ledger(self):
        commits = merge_flow.parse_git_log(git_log_text(
            ("a", NOW - 10, "Implement issue #1: v (#10)",
             "* WIP: preserve interrupted work for issue #1\n"
             "* Implement issue #1: v\n* Implement issue #1: v\n"),
        ))
        jobs = {1: job(1, NOW - 5000, NOW - 10, repairs=2, attempt=3)}
        report = merge_flow.window_report(
            commits, NOW - WEEK, NOW, {1: issue(1, state="CLOSED")}, jobs)
        repair = report["repair_incidence"]
        self.assertEqual(repair["merges_with_wip_preserve"], 1)
        self.assertEqual(repair["wip_preserve_commits"], 1)
        self.assertEqual(repair["merges_with_repeated_work_commits"], 1)
        self.assertEqual(repair["ledger_repairs"], 1)
        self.assertEqual(repair["ledger_redispatched"], 1)

    def test_degenerate_empty_window(self):
        report = merge_flow.window_report([], NOW - WEEK, NOW, {}, {})
        self.assertEqual(report["merges"], 0)
        self.assertEqual(report["areas"], {})
        self.assertIsNone(report["lead_time_hours"]["p50"])
        self.assertEqual(report["lead_time_hours"]["count"], 0)


class BacklogTests(unittest.TestCase):
    def test_ready_blocked_share_and_dependency_blocked(self):
        issues = [
            issue(1, labels=("agent:ready",)),
            issue(2, labels=("agent:ready",), deps=(3,)),
            issue(3, state="CLOSED"),
            issue(4, labels=("agent:blocked",)),
            issue(5, labels=("agent:blocked",)),
            issue(6, labels=("agent:working",)),
            issue(7),
        ]
        report = merge_flow.backlog_report(issues)
        self.assertEqual(report["open"], 6)
        self.assertEqual(report["by_label"],
                         {"blocked": 2, "ready": 2, "unlabeled": 1, "working": 1})
        self.assertEqual(report["ready_share"], round(2 / 6, 2))
        self.assertEqual(report["blocked_share"], round(2 / 6, 2))
        # issue 2's dependency 3 is closed; nothing blocks either ready issue
        self.assertEqual(report["ready_dependency_blocked"], 0)

    def test_ready_issue_with_open_dependency_counts(self):
        issues = [issue(1, labels=("agent:ready",), deps=(2,)), issue(2)]
        self.assertEqual(merge_flow.backlog_report(issues)["ready_dependency_blocked"], 1)

    def test_empty_backlog(self):
        report = merge_flow.backlog_report([])
        self.assertEqual(report["open"], 0)
        self.assertIsNone(report["ready_share"])


class BuildReportTests(unittest.TestCase):
    def test_full_report_shape_and_delta(self):
        boundary = NOW - WEEK
        commits = merge_flow.parse_git_log(git_log_text(
            ("a", NOW - 100, "Implement issue #1: a (#10)", ""),
            ("b", boundary - 100, "Implement issue #2: b (#11)", ""),
            ("c", boundary - 200, "Implement issue #3: c (#12)", ""),
            ("z", NOW - 3 * WEEK, "old history (#9)", ""),
        ))
        issues = [issue(1, area="operations", state="CLOSED"),
                  issue(2, area="library", state="CLOSED"),
                  issue(3, area="library", state="CLOSED")]
        jobs = [job(1, NOW - 3700, NOW - 100),
                job(2, boundary - 1000, boundary - 100),
                job(3, boundary - 2000, boundary - 200)]
        report = merge_flow.build_report(commits, issues, jobs, NOW, 7)
        self.assertEqual(report["windows"]["current"]["merges"], 1)
        self.assertEqual(report["windows"]["previous"]["merges"], 2)
        self.assertEqual(report["merge_decline_percent"], 50.0)
        self.assertEqual(report["windows"]["current"]["areas"], {"operations": 1})
        self.assertEqual(report["ledger"]["ledger_merges"], {"previous": 2, "current": 1})
        self.assertTrue(report["history"]["covers_window_pair"])
        self.assertIsNotNone(report["backlog"])

    def test_no_previous_merges_gives_null_delta(self):
        commits = merge_flow.parse_git_log(git_log_text(
            ("a", NOW - 100, "x (#1)", ""),))
        report = merge_flow.build_report(commits, [], [], NOW, 7)
        self.assertIsNone(report["merge_decline_percent"])
        self.assertEqual(report["backlog"]["open"], 0)

    def test_deterministic_for_same_inputs(self):
        commits = merge_flow.parse_git_log(git_log_text(
            ("a", NOW - 100, "Implement issue #1: a (#10)", ""),))
        issues = [issue(1, area="operations", state="CLOSED")]
        jobs = [job(1, NOW - 4000, NOW - 100)]
        self.assertEqual(merge_flow.build_report(commits, issues, jobs, NOW, 7),
                         merge_flow.build_report(commits, issues, jobs, NOW, 7))


class CliTests(unittest.TestCase):
    def test_main_with_injected_files_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "log.txt").write_text(git_log_text(
                ("a", NOW - 100, "Implement issue #1: a (#10)", "")))
            (tmp / "issues.json").write_text(json.dumps(
                [issue(1, area="operations", state="CLOSED")]))
            (tmp / "jobs.json").write_text(json.dumps(
                [job(1, NOW - 4000, NOW - 100)]))
            out = io.StringIO()
            argv = ["--git-log", str(tmp / "log.txt"),
                    "--issues", str(tmp / "issues.json"),
                    "--jobs", str(tmp / "jobs.json"),
                    "--now", str(NOW), "--json"]
            with contextlib.redirect_stdout(out):
                self.assertEqual(merge_flow.main(argv), 0)
            first = json.loads(out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(merge_flow.main(argv), 0)
            self.assertEqual(first, json.loads(out.getvalue()))
            self.assertEqual(first["windows"]["current"]["merges"], 1)
            self.assertEqual(first["ref"], "origin/main")


if __name__ == "__main__":
    unittest.main()
