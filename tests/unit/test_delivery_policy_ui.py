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
