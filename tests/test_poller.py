import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
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
    def test_is_actionable_default_reasons(self):
        self.assertTrue(poller.is_actionable({"reason": "assign"}, poller.DEFAULT_REASONS))
        self.assertTrue(poller.is_actionable({"reason": "mention"}, poller.DEFAULT_REASONS))
        self.assertTrue(poller.is_actionable({"reason": "review_requested"}, poller.DEFAULT_REASONS))
        self.assertTrue(poller.is_actionable({"reason": "comment"}, poller.DEFAULT_REASONS))
        self.assertFalse(poller.is_actionable({"reason": "ci_activity"}, poller.DEFAULT_REASONS))

    def test_safe_thread_id(self):
        self.assertEqual(poller.safe_thread_id({"id": "abc/123:xyz"}), "abc_123_xyz")

    def test_spool_contains_required_context(self):
        n = {
            "id": "123",
            "reason": "assign",
            "repository": {"full_name": "owner/repo"},
            "subject": {"type": "Issue", "title": "Bug", "url": "https://api.github.com/repos/owner/repo/issues/1"},
            "url": "https://api.github.com/notifications/threads/123",
        }
        spool = poller.build_spool(n, bot_login="bot", reviewer="reviewer")
        self.assertEqual(spool["thread_id"], "123")
        self.assertEqual(spool["repository"], "owner/repo")
        self.assertEqual(spool["bot_login"], "bot")
        self.assertEqual(spool["reviewer"], "reviewer")
        self.assertEqual(spool["notification"], n)

    def test_process_notification_order_spool_mark_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            dirs = poller.ensure_dirs(state)
            args = poller.parse_args(["--state-dir", str(state), "--dry-run"])
            calls = []

            def fake_mark(gh, thread_id, dry_run=False):
                calls.append(("mark", thread_id, (dirs["pending"] / "123.json").exists()))

            def fake_spawn(**kwargs):
                calls.append(("spawn", kwargs["spool_path"].exists()))
                return None

            with patch.object(poller, "mark_done", fake_mark), patch.object(poller, "spawn_agent", fake_spawn):
                poller.process_notification({"id": "123", "reason": "assign"}, args=args, dirs=dirs)

            self.assertEqual(calls[0], ("mark", "123", True))
            self.assertEqual(calls[1], ("spawn", True))
            self.assertFalse((dirs["pending"] / "123.json").exists())
            self.assertTrue((dirs["claimed"] / "123.json").exists())

    def test_main_silent_when_no_notifications(self):
        with tempfile.TemporaryDirectory() as td, patch.object(poller, "fetch_notifications", return_value=[]):
            with patch("sys.stdout") as stdout:
                code = poller.main(["--state-dir", td])
            self.assertEqual(code, 0)
            stdout.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
