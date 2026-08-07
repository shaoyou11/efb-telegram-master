import inspect
from types import SimpleNamespace
from unittest.mock import patch

from ehforwarderbot import coordinator
from efb_telegram_master.commands import CommandsManager
from efb_telegram_master.wechat_control import (
    COMWECHAT_CHANNEL_ID,
    WeChatControl,
    confirmation_keyboard,
    find_comwechat_channel,
    panel_keyboard,
)


def test_finds_comwechat_by_stable_channel_id_instead_of_position():
    unrelated = SimpleNamespace(channel_id="tests.unrelated")
    comwechat = SimpleNamespace(channel_id=COMWECHAT_CHANNEL_ID)

    result = find_comwechat_channel({
        unrelated.channel_id: unrelated,
        comwechat.channel_id: comwechat,
    })

    assert result is comwechat


def test_login_alias_calls_existing_comwechat_extra_function():
    comwechat = SimpleNamespace(
        channel_id=COMWECHAT_CHANNEL_ID,
        get_extra_functions=lambda: {"reauth": lambda argument: f"登录:{argument}"},
    )

    with patch.dict(coordinator.slaves, {COMWECHAT_CHANNEL_ID: comwechat}, clear=True):
        result = WeChatControl.call_extra("reauth")

    assert result == "登录:"


def test_panel_is_chinese_and_uses_stable_callbacks():
    rows = panel_keyboard().inline_keyboard

    assert [row[0].text for row in rows] == [
        "重新扫码登录",
        "强制退出微信",
        "自动恢复设置",
        "关闭",
    ]
    assert [row[0].callback_data for row in rows] == [
        "wechat:login",
        "wechat:logout",
        "wechat:watchdog",
        "wechat:close",
    ]


def test_force_logout_requires_confirmation():
    rows = confirmation_keyboard().inline_keyboard

    assert rows[0][0].text == "确认退出微信"
    assert rows[0][0].callback_data == "wechat:logout_confirm"
    assert rows[1][0].text == "取消"
    assert rows[1][0].callback_data == "wechat:cancel"


def test_extra_command_redirects_to_wechat_panel():
    source = inspect.getsource(CommandsManager.extra_listing)

    assert "wechat_control.show" in source


def test_login_prompt_is_tracked_and_deleted_after_successful_login():
    deleted = []

    class FakeStatus:
        chat_id = 123
        message_id = 456

        def edit_text(self, text):
            self.text = text

    class FakeMessage:
        def reply_text(self, text):
            status = FakeStatus()
            status.text = text
            return status

    control = WeChatControl.__new__(WeChatControl)
    control.channel = SimpleNamespace(
        bot_manager=SimpleNamespace(
            delete_message=lambda chat_id, message_id: deleted.append(
                (chat_id, message_id)
            )
        )
    )
    control._login_prompt_messages = set()

    with patch.object(WeChatControl, "call_extra", return_value="请扫描二维码登录"):
        control.run_action(FakeMessage(), "reauth", "正在获取二维码")

    assert control._login_prompt_messages == {(123, 456)}
    assert control.cleanup_login_prompts() == 1
    assert deleted == [(123, 456)]
    assert control._login_prompt_messages == set()
