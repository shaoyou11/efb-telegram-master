import importlib.util
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "efb_telegram_master" / "bridge_dead_letter.py"
SPEC = importlib.util.spec_from_file_location("bridge_dead_letter", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BridgeDeadLetterGuard = MODULE.BridgeDeadLetterGuard
alert_text = MODULE.alert_text


class Dispatcher:
    def add_handler(self, _handler):
        pass


class BotManager:
    def __init__(self):
        self.dispatcher = Dispatcher()
        self.sent = []

    def send_message(self, admin, text, reply_markup=None):
        self.sent.append((admin, text, reply_markup))


def test_alert_does_not_include_message_payload_or_path():
    text = alert_text({"attempts": 10, "last_error": "FileNotFoundError"})
    assert "已尝试：10 次" in text
    assert "FileNotFoundError" in text
    assert "/comwechat/Files" not in text


def test_dead_letter_alert_is_persistent_and_only_sent_once(tmp_path):
    channel = SimpleNamespace(
        config={"admins": [123]},
        bot_manager=BotManager(),
    )
    guard = BridgeDeadLetterGuard(
        channel,
        state_path=tmp_path / "alerts.json",
        autostart=False,
    )
    guard._json = lambda *_args, **_kwargs: {
        "messages": [{"id": "dead-1", "attempts": 10, "last_error": "missing"}]
    }

    guard.check_once()
    guard.check_once()

    assert len(channel.bot_manager.sent) == 1
    buttons = channel.bot_manager.sent[0][2].inline_keyboard[0]
    assert [button.text for button in buttons] == ["重新投递", "关闭页面"]
    restored = BridgeDeadLetterGuard(
        channel,
        state_path=tmp_path / "alerts.json",
        autostart=False,
    )
    assert restored.notified == {"dead-1"}
