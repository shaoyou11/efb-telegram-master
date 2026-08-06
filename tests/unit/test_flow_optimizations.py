import tempfile
import threading
import unittest
import importlib.util
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_module(name):
    path = PROJECT_ROOT / "efb_telegram_master" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


delivery_scheduler = load_module("delivery_scheduler")
delivery_trace = load_module("delivery_trace")
digest = load_module("digest")
issues = load_module("issues")
content_parser = load_module("content_parser")

DeliveryScheduler = delivery_scheduler.DeliveryScheduler
TelegramRateLimiter = delivery_scheduler.TelegramRateLimiter
DeliveryTraceStore = delivery_trace.DeliveryTraceStore
DigestStore = digest.DigestStore
build_issues = issues.build_issues
audit_chat_mappings = issues.audit_chat_mappings
normalize_wechat_html = content_parser.normalize_wechat_html


class FlowOptimizationTests(unittest.TestCase):
    def test_trace_store_keeps_ordered_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryTraceStore(Path(directory) / "trace.json")
            store.record("message-1", "received", chat="contact")
            store.record("message-1", "telegram_ack", target="100")
            stages = [item["stage"] for item in store.get("message-1")]
        self.assertEqual(stages, ["received", "telegram_ack"])

    def test_digest_store_is_opt_in_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "digest.json"
            store = DigestStore(path)
            self.assertFalse(store.enabled("tg:1:2"))
            store.set_enabled("tg:1:2", True)
            restored = DigestStore(path)
        self.assertTrue(restored.enabled("tg:1:2"))

    def test_issue_list_exposes_recipient_and_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "operations" / "state"
            state.mkdir(parents=True)
            (state / "failed-deliveries.json").write_text(
                '{"items":{"abc":{"tg_dest":123,"thread_id":4,"path":"/tmp/file","error":"missing file"}}}',
                encoding="utf-8",
            )
            issues = build_issues(root)
        self.assertEqual(issues[0]["recipient"], "123/4")
        self.assertEqual(issues[0]["action"], "retry")

    def test_scheduler_preserves_same_chat_order(self):
        scheduler = DeliveryScheduler(worker_count=1, autostart=False)
        calls = []
        first = scheduler.submit("contact", True, lambda: calls.append("first"))
        second = scheduler.submit("contact", True, lambda: calls.append("second"))
        scheduler.start()
        first.result(timeout=2)
        second.result(timeout=2)
        scheduler.close()
        self.assertEqual(calls, ["first", "second"])

    def test_rate_limiter_has_bounded_burst(self):
        limiter = TelegramRateLimiter(rate_per_second=100, burst=2)
        self.assertTrue(limiter.acquire(timeout=0))
        self.assertTrue(limiter.acquire(timeout=0))

    def test_content_parser_keeps_safe_links_and_readable_unsupported_cards(self):
        text = '<a href="https://example.invalid/a?x=1">打开网页</a><br><a href="weixin://kefumenu?id=1">微信菜单</a>'
        rendered = normalize_wechat_html(text)
        self.assertIn('href="https://example.invalid/a?x=1"', rendered)
        self.assertIn("打开网页", rendered)
        self.assertIn("微信菜单", rendered)
        self.assertNotIn("weixin://", rendered)

    def test_mapping_audit_reports_malformed_topic(self):
        class FakeDB:
            @staticmethod
            def get_all_chat_assocs():
                return {"blueset.telegram -100": ["honus.comwechat wxid"]}

            @staticmethod
            def get_all_topic_assocs():
                return {-100: ["honus.comwechat wxid"]}

            @staticmethod
            def get_topic_slaves(_chat_id):
                return [("honus.comwechat wxid", "bad-thread")]

        findings = audit_chat_mappings(FakeDB())
        self.assertTrue(any(item["kind"] == "话题映射" for item in findings))


if __name__ == "__main__":
    unittest.main()
