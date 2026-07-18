from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot import coordinator
from telegram import InlineKeyboardMarkup

from efb_telegram_master.delivery_policy import DeliveryPolicy
from efb_telegram_master.delivery_policy_ui import (
    build_list_navigation,
    build_policy_keyboard,
    parse_filter_action,
)


def callback_values(markup: InlineKeyboardMarkup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_policy_keyboard_contains_all_policies_reset_back_and_close():
    callbacks = callback_values(build_policy_keyboard(DeliveryPolicy.NORMAL))

    assert "filter:set:normal" in callbacks
    assert "filter:set:silent" in callbacks
    assert "filter:set:filtered" in callbacks
    assert "filter:reset" in callbacks
    assert "filter:back" in callbacks
    assert "filter:close" in callbacks


def test_list_navigation_always_contains_close():
    callbacks = callback_values(build_list_navigation(0, 10, 25))

    assert "filter:page:10" in callbacks
    assert "filter:close" in callbacks


def test_filter_action_parser_rejects_unrelated_callbacks():
    assert parse_filter_action("filter:chat:3") == ("chat", "3")
    assert parse_filter_action("filter:close") == ("close", "")
    assert parse_filter_action("cleanup:close") is None


def test_filter_chat_list_refreshes_from_slave(monkeypatch):
    remote_chat = SimpleNamespace(long_name="微信会话", last_message_time=1)
    cached_chats = []
    chat_manager = Mock()
    type(chat_manager).all_chats = property(lambda _: iter(cached_chats))
    chat_manager.update_chat_obj.side_effect = lambda chat, full_update: cached_chats.append(chat)
    channel = SimpleNamespace(chat_manager=chat_manager, delivery_policy_store=Mock())
    slave = SimpleNamespace(channel_id="honus.comwechat", get_chats=lambda: [remote_chat])
    monkeypatch.setattr(coordinator, "slaves", {slave.channel_id: slave})

    from efb_telegram_master.delivery_policy_ui import DeliveryPolicyUI
    chats = DeliveryPolicyUI(channel)._all_chats()

    assert chats == [remote_chat]
    chat_manager.update_chat_obj.assert_called_once_with(remote_chat, full_update=True)


def test_filter_chat_list_uses_cache_without_refreshing_slave(monkeypatch):
    cached_chat = SimpleNamespace(long_name="已有会话", last_message_time=1)
    chat_manager = Mock()
    type(chat_manager).all_chats = property(lambda _: iter([cached_chat]))
    channel = SimpleNamespace(chat_manager=chat_manager, delivery_policy_store=Mock())
    slave = Mock(channel_id="honus.comwechat")
    monkeypatch.setattr(coordinator, "slaves", {slave.channel_id: slave})

    from efb_telegram_master.delivery_policy_ui import DeliveryPolicyUI
    chats = DeliveryPolicyUI(channel)._all_chats()

    assert chats == [cached_chat]
    slave.get_chats.assert_not_called()


def test_filter_resolves_single_linked_group_as_current_chat():
    remote_chat = SimpleNamespace(uid="gh_news", module_id="honus.comwechat")
    chat_manager = Mock()
    chat_manager.get_chat.return_value = remote_chat
    db = Mock()
    db.get_chat_assoc.return_value = ["honus.comwechat gh_news"]
    channel = SimpleNamespace(
        channel_id="blueset.telegram",
        chat_manager=chat_manager,
        delivery_policy_store=Mock(),
        db=db,
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100123, type="supergroup", is_forum=False),
        effective_message=SimpleNamespace(message_thread_id=None),
    )

    from efb_telegram_master.delivery_policy_ui import DeliveryPolicyUI
    chat = DeliveryPolicyUI(channel)._context_chat(update)

    assert chat is remote_chat
    chat_manager.get_chat.assert_called_once_with("honus.comwechat", "gh_news")


def test_filter_views_select_policy_and_public_account():
    store = Mock()
    store.get.side_effect = [DeliveryPolicy.SILENT, DeliveryPolicy.FILTERED]
    channel = SimpleNamespace(chat_manager=Mock(), delivery_policy_store=store)
    ui = DeliveryPolicyUI(channel)
    public = SimpleNamespace(vendor_specific={"is_mp": True})
    private = SimpleNamespace(vendor_specific={})

    assert ui._view_chats([public, private], "silent") == [public]
    assert ui._view_chats([public, private], "mp") == [public]
