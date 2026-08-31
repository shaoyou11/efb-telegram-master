from types import SimpleNamespace
from unittest.mock import Mock, patch

from efb_telegram_master.wechat_read_ui import WechatReadUI


def test_global_auto_read_defaults_off_and_persists(tmp_path):
    state_path = tmp_path / "wechat-read-settings.json"
    channel = SimpleNamespace(config={"admins": [1]})

    settings = WechatReadUI(channel, state_path=state_path)
    assert settings.enabled is False

    settings.set_enabled(True)

    assert WechatReadUI(channel, state_path=state_path).enabled is True


def test_enabled_auto_read_marks_incoming_comwechat_chat(tmp_path):
    channel = SimpleNamespace(config={"admins": [1]})
    settings = WechatReadUI(
        channel,
        state_path=tmp_path / "wechat-read-settings.json",
    )
    settings.set_enabled(True)
    slave = Mock()
    message = SimpleNamespace(
        chat=SimpleNamespace(module_id="honus.comwechat", uid="chat-a"),
        author=SimpleNamespace(),
    )

    with patch(
        "efb_telegram_master.wechat_read_ui.coordinator.get_module_by_id",
        return_value=slave,
    ):
        assert settings.mark_message_read(message) is True

    slave.mark_wechat_read.assert_called_once_with("chat-a")


def test_disabled_auto_read_does_not_call_slave(tmp_path):
    channel = SimpleNamespace(config={"admins": [1]})
    settings = WechatReadUI(
        channel,
        state_path=tmp_path / "wechat-read-settings.json",
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(module_id="honus.comwechat", uid="chat-a"),
        author=SimpleNamespace(),
    )

    with patch(
        "efb_telegram_master.wechat_read_ui.coordinator.get_module_by_id",
    ) as get_module:
        assert settings.mark_message_read(message) is False

    get_module.assert_not_called()


def test_auto_read_failure_does_not_break_delivery(tmp_path):
    channel = SimpleNamespace(config={"admins": [1]})
    settings = WechatReadUI(
        channel,
        state_path=tmp_path / "wechat-read-settings.json",
    )
    settings.set_enabled(True)
    slave = Mock()
    slave.mark_wechat_read.side_effect = RuntimeError("failed")
    message = SimpleNamespace(
        chat=SimpleNamespace(module_id="honus.comwechat", uid="chat-a"),
        author=SimpleNamespace(),
    )

    with patch(
        "efb_telegram_master.wechat_read_ui.coordinator.get_module_by_id",
        return_value=slave,
    ):
        assert settings.mark_message_read(message) is False
