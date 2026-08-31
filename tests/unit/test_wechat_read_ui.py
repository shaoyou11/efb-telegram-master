from types import SimpleNamespace
from unittest.mock import Mock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from efb_telegram_master.wechat_read_ui import (
    READ_CALLBACK,
    WechatReadUI,
    append_read_button,
    remove_read_button,
)


def test_read_button_preserves_existing_markup_and_can_be_removed():
    existing = InlineKeyboardMarkup([[InlineKeyboardButton("原按钮", callback_data="old")]])

    marked = append_read_button(existing)
    cleaned = remove_read_button(marked)

    assert [button.callback_data for row in marked.inline_keyboard for button in row] == [
        "old",
        READ_CALLBACK,
    ]
    assert [button.callback_data for row in cleaned.inline_keyboard for button in row] == ["old"]


def test_non_admin_cannot_mark_wechat_read():
    channel = SimpleNamespace(config={"admins": [1]}, db=Mock())
    ui = WechatReadUI(channel)
    query = Mock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        callback_query=query,
    )

    ui.callback(update, None)

    channel.db.get_msg_log.assert_not_called()
    query.answer.assert_called_once()


def test_admin_mark_read_uses_message_mapping_and_removes_only_read_button():
    channel = SimpleNamespace(config={"admins": [1]}, db=Mock())
    channel.db.get_msg_log.return_value = SimpleNamespace(
        slave_origin_uid="honus.comwechat chat-a"
    )
    slave = Mock()
    query = Mock()
    query.message.chat_id = 100
    query.message.message_id = 200
    query.message.reply_markup = append_read_button(
        InlineKeyboardMarkup([[InlineKeyboardButton("原按钮", callback_data="old")]])
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        callback_query=query,
    )

    with patch(
        "efb_telegram_master.wechat_read_ui.coordinator.get_module_by_id",
        return_value=slave,
    ):
        WechatReadUI(channel).callback(update, None)

    slave.mark_wechat_read.assert_called_once_with("chat-a")
    callbacks = [
        button.callback_data
        for row in query.edit_message_reply_markup.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["old"]


def test_reply_auto_read_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EFB_WECHAT_READ_ON_REPLY", raising=False)
    channel = SimpleNamespace(config={"admins": [1]})

    assert WechatReadUI(channel).mark_destination_read(
        "honus.comwechat chat-a"
    ) is False


def test_reply_auto_read_marks_only_comwechat_destination(monkeypatch):
    monkeypatch.setenv("EFB_WECHAT_READ_ON_REPLY", "true")
    channel = SimpleNamespace(config={"admins": [1]})
    slave = Mock()

    with patch(
        "efb_telegram_master.wechat_read_ui.coordinator.get_module_by_id",
        return_value=slave,
    ):
        marked = WechatReadUI(channel).mark_destination_read(
            "honus.comwechat chat-a"
        )

    assert marked is True
    slave.mark_wechat_read.assert_called_once_with("chat-a")
