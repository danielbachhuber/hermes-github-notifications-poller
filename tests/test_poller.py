import argparse
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "hermes-github-notifications-poller"
loader = SourceFileLoader("poller", str(SCRIPT))
spec = importlib.util.spec_from_loader("poller", loader)
assert spec is not None
poller = importlib.util.module_from_spec(spec)
loader.exec_module(poller)


class PollerTests(unittest.TestCase):
    def notification(self, *, id="123", reason="assign", repo="owner/repo", subject_type="Issue"):
        return {
            "id": id,
            "reason": reason,
            "repository": {"full_name": repo},
            "subject": {
                "type": subject_type,
                "title": "Important work",
                "url": f"https://api.github.com/repos/{repo}/issues/1",
            },
            "url": f"https://api.github.com/notifications/threads/{id}",
            "subscription_url": f"https://api.github.com/notifications/threads/{id}/subscription",
        }

    def test_is_actionable_default_reasons(self):
        for reason in ["assign", "mention", "review_requested", "team_mention", "comment"]:
            with self.subTest(reason=reason):
                self.assertTrue(poller.is_actionable({"reason": reason}, poller.DEFAULT_REASONS))
        for reason in ["ci_activity", "state_change", "subscribed", "security_alert", None]:
            with self.subTest(reason=reason):
                self.assertFalse(poller.is_actionable({"reason": reason}, poller.DEFAULT_REASONS))

    def test_safe_thread_id(self):
        self.assertEqual(poller.safe_thread_id({"id": "abc/123:xyz"}), "abc_123_xyz")
        self.assertEqual(poller.safe_thread_id({}), "unknown")

    def test_build_spool_contains_required_context_with_optional_config(self):
        n = self.notification()
        spool = poller.build_spool(n, bot_login="bot", reviewer="reviewer")
        self.assertEqual(spool["schema_version"], 1)
        self.assertEqual(spool["thread_id"], "123")
        self.assertEqual(spool["reason"], "assign")
        self.assertEqual(spool["repository"], "owner/repo")
        self.assertEqual(spool["subject"], n["subject"])
        self.assertEqual(spool["notification_url"], n["url"])
        self.assertEqual(spool["subscription_url"], n["subscription_url"])
        self.assertEqual(spool["bot_login"], "bot")
        self.assertEqual(spool["reviewer"], "reviewer")
        self.assertEqual(spool["notification"], n)
        self.assertIn("received_at", spool)

    def test_build_spool_omits_default_reviewer_when_not_configured(self):
        spool = poller.build_spool(self.notification(), bot_login=None, reviewer=None)
        self.assertIsNone(spool["bot_login"])
        self.assertNotIn("reviewer", spool)

    def test_agent_prompt_without_installation_specific_defaults(self):
        prompt = poller.build_agent_prompt(Path("/tmp/spool.json"), bot_login=None, reviewer=None)
        self.assertIn("infer from `gh api user`", prompt)
        self.assertIn("Do not request a default reviewer", prompt)
        self.assertIn("Decision matrix", prompt)
        self.assertIn("address review comments incrementally", prompt)
        self.assertIn("Do not open a second PR", prompt)
        self.assertIn("do nothing on GitHub", prompt)
        old_bot = "otto" + "bachhuber" + "bot"
        old_reviewer = "daniel" + "bachhuber"
        self.assertNotIn(old_bot, prompt)
        self.assertNotIn(old_reviewer, prompt)

    def test_agent_prompt_includes_configured_bot_and_reviewer(self):
        prompt = poller.build_agent_prompt(Path("/tmp/spool.json"), bot_login="my-bot", reviewer="reviewer-user")
        self.assertIn("Bot account: my-bot", prompt)
        self.assertIn("Default PR reviewer: reviewer-user", prompt)
        self.assertIn("Request review from `reviewer-user`", prompt)
        self.assertIn("assigned to `my-bot`", prompt)

    def test_parse_args_uses_environment_configuration(self):
        with patch.dict("os.environ", {"GITHUB_BOT_LOGIN": "env-bot", "GITHUB_DEFAULT_REVIEWER": "env-reviewer"}, clear=False):
            args = poller.parse_args([])
        self.assertEqual(args.bot_login, "env-bot")
        self.assertEqual(args.reviewer, "env-reviewer")

    def test_parse_args_has_no_installation_specific_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            args = poller.parse_args([])
        self.assertIsNone(args.bot_login)
        self.assertIsNone(args.reviewer)

    def test_parse_args_cli_overrides_environment_configuration(self):
        with patch.dict("os.environ", {"GITHUB_BOT_LOGIN": "env-bot", "GITHUB_DEFAULT_REVIEWER": "env-reviewer"}, clear=False):
            args = poller.parse_args(["--bot-login", "cli-bot", "--reviewer", "cli-reviewer"])
        self.assertEqual(args.bot_login, "cli-bot")
        self.assertEqual(args.reviewer, "cli-reviewer")

    def test_fetch_notifications_parses_single_json_array(self):
        payload = [self.notification(id="1"), self.notification(id="2")]
        completed = subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(poller.subprocess, "run", return_value=completed) as run:
            self.assertEqual(poller.fetch_notifications("gh"), payload)
        run.assert_called_once_with(["gh", "api", "notifications", "--paginate"], text=True, capture_output=True, timeout=120)

    def test_fetch_notifications_parses_paginated_json_lines_arrays(self):
        page1 = [self.notification(id="1")]
        page2 = [self.notification(id="2")]
        completed = subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(page1) + "\n" + json.dumps(page2), stderr="")
        with patch.object(poller.subprocess, "run", return_value=completed):
            self.assertEqual(poller.fetch_notifications("gh"), page1 + page2)

    def test_fetch_notifications_raises_on_gh_failure(self):
        completed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="bad auth")
        with patch.object(poller.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "failed to fetch notifications"):
                poller.fetch_notifications("gh")

    def test_mark_done_deletes_notification_thread(self):
        completed = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
        with patch.object(poller.subprocess, "run", return_value=completed) as run:
            poller.mark_done("gh", "123", dry_run=False)
        run.assert_called_once_with(
            ["gh", "api", "--method", "DELETE", "/notifications/threads/123"],
            text=True,
            capture_output=True,
            timeout=60,
        )

    def test_mark_done_skips_in_dry_run(self):
        with patch.object(poller.subprocess, "run") as run:
            poller.mark_done("gh", "123", dry_run=True)
        run.assert_not_called()

    def test_mark_done_raises_on_failure(self):
        completed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="nope")
        with patch.object(poller.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "failed to mark notification 123 done"):
                poller.mark_done("gh", "123", dry_run=False)

    def test_process_notification_order_spool_mark_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            dirs = poller.ensure_dirs(state)
            args = poller.parse_args(["--state-dir", str(state), "--dry-run", "--bot-login", "bot"])
            calls = []

            def fake_mark(gh, thread_id, dry_run=False):
                calls.append(("mark", thread_id, (dirs["pending"] / "123.json").exists()))

            def fake_spawn(**kwargs):
                calls.append(("spawn", kwargs["spool_path"].exists(), kwargs["bot_login"]))
                return None

            with patch.object(poller, "mark_done", fake_mark), patch.object(poller, "spawn_agent", fake_spawn):
                thread_id, claimed_path = poller.process_notification(self.notification(), args=args, dirs=dirs)

            self.assertEqual(thread_id, "123")
            self.assertEqual(calls[0], ("mark", "123", True))
            self.assertEqual(calls[1], ("spawn", True, "bot"))
            self.assertFalse((dirs["pending"] / "123.json").exists())
            self.assertEqual(Path(claimed_path), dirs["claimed"] / "123.json")
            self.assertTrue((dirs["claimed"] / "123.json").exists())

    def test_process_notification_moves_to_failed_if_spawn_fails_after_mark_done(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            dirs = poller.ensure_dirs(state)
            args = poller.parse_args(["--state-dir", str(state)])
            with patch.object(poller, "mark_done") as mark_done, patch.object(poller, "spawn_agent", side_effect=RuntimeError("spawn failed")):
                with self.assertRaisesRegex(RuntimeError, "spawn failed"):
                    poller.process_notification(self.notification(), args=args, dirs=dirs)
            mark_done.assert_called_once()
            self.assertFalse((dirs["pending"] / "123.json").exists())
            self.assertFalse((dirs["claimed"] / "123.json").exists())
            self.assertTrue((dirs["failed"] / "123.json").exists())

    def test_spawn_agent_command_loads_skills_before_chat_and_logs(self):
        with tempfile.TemporaryDirectory() as td:
            dirs = poller.ensure_dirs(Path(td))
            spool = dirs["claimed"] / "123.json"
            spool.write_text("{}", encoding="utf-8")
            fake_proc = Mock()
            with patch.object(poller.subprocess, "Popen", return_value=fake_proc) as popen:
                result = poller.spawn_agent(
                    hermes="hermes",
                    spool_path=spool,
                    dirs=dirs,
                    bot_login="bot",
                    reviewer=None,
                    dry_run=False,
                    extra_args=["--model", "test-model"],
                )
            self.assertIs(result, fake_proc)
            cmd = popen.call_args.args[0]
            self.assertEqual(cmd[0], "hermes")
            self.assertLess(cmd.index("--skills"), cmd.index("chat"))
            self.assertIn("github-notification-poller", cmd)
            self.assertIn("--model", cmd)
            self.assertIn("test-model", cmd)
            self.assertTrue((Path(td) / "logs" / "123.log").exists())

    def test_spawn_agent_does_nothing_in_dry_run(self):
        with tempfile.TemporaryDirectory() as td, patch.object(poller.subprocess, "Popen") as popen:
            dirs = poller.ensure_dirs(Path(td))
            result = poller.spawn_agent(
                hermes="hermes",
                spool_path=dirs["claimed"] / "123.json",
                dirs=dirs,
                bot_login=None,
                reviewer=None,
                dry_run=True,
                extra_args=[],
            )
        self.assertIsNone(result)
        popen.assert_not_called()

    def test_main_silent_when_no_notifications(self):
        with tempfile.TemporaryDirectory() as td, patch.object(poller, "fetch_notifications", return_value=[]):
            with patch("sys.stdout") as stdout:
                code = poller.main(["--state-dir", td])
            self.assertEqual(code, 0)
            stdout.write.assert_not_called()

    def test_main_filters_notifications_and_processes_only_actionable(self):
        notifications = [self.notification(id="1", reason="assign"), self.notification(id="2", reason="ci_activity")]
        with tempfile.TemporaryDirectory() as td, patch.object(poller, "fetch_notifications", return_value=notifications), patch.object(poller, "process_notification", return_value=("1", "claimed/1.json")) as process:
            code = poller.main(["--state-dir", td])
        self.assertEqual(code, 0)
        process.assert_called_once()
        self.assertEqual(process.call_args.args[0]["id"], "1")

    def test_main_custom_reason_filter(self):
        notifications = [self.notification(id="1", reason="ci_activity"), self.notification(id="2", reason="assign")]
        with tempfile.TemporaryDirectory() as td, patch.object(poller, "fetch_notifications", return_value=notifications), patch.object(poller, "process_notification", return_value=("1", "claimed/1.json")) as process:
            code = poller.main(["--state-dir", td, "--reason", "ci_activity"])
        self.assertEqual(code, 0)
        process.assert_called_once()
        self.assertEqual(process.call_args.args[0]["id"], "1")

    def test_main_verbose_prints_summary_only_when_requested(self):
        with tempfile.TemporaryDirectory() as td, patch.object(poller, "fetch_notifications", return_value=[]):
            with patch("builtins.print") as print_mock:
                code = poller.main(["--state-dir", td, "--verbose"])
        self.assertEqual(code, 0)
        print_mock.assert_called_once()
        summary = json.loads(print_mock.call_args.args[0])
        self.assertEqual(summary["fetched"], 0)
        self.assertEqual(summary["actionable"], 0)

    def test_main_logs_and_alerts_on_error(self):
        with tempfile.TemporaryDirectory() as td, patch.object(poller, "fetch_notifications", side_effect=RuntimeError("boom")):
            with patch("sys.stderr"):
                code = poller.main(["--state-dir", td])
            log = (Path(td) / "poller.log").read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertIn("ERROR RuntimeError: boom", log)


if __name__ == "__main__":
    unittest.main()
