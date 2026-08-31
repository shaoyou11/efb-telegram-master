from types import SimpleNamespace
from unittest.mock import Mock

from pytest import fixture
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from ehforwarderbot import Message, Chat, MsgType
from ehforwarderbot.types import ReactionName
from efb_telegram_master.constants import Emoji
from efb_telegram_master.delivery_policy import DeliveryPolicy
from efb_telegram_master.slave_message import SlaveMessageProcessor


def test_slave_message_reaction_footer(slave):
    # No content should be returned if no reaction is available
    assert not SlaveMessageProcessor.build_reactions_footer({})

    # Footer should contain the reaction name and number of reactors
    reactions = {
        ReactionName("__reaction_a__"):
            [slave.chat_with_alias, slave.chat_without_alias],
        ReactionName("__reaction_b__"):
            [slave.chat_with_alias],
        ReactionName("__reaction_c__"): []
    }
    footer = SlaveMessageProcessor.build_reactions_footer(reactions)
    assert "__reaction_a__" in footer
    assert "2" in footer
    assert "__reaction_b__" in footer
    assert "1" in footer
    assert "__reaction_c__" not in footer

    # Footer should be empty if no reaction name gives any value.
    footer = SlaveMessageProcessor.build_reactions_footer({
        ReactionName("__reaction_x__"): []
    })
    assert not footer


@fixture(scope="module")
def generate_message_template(channel):
    return channel.slave_messages.generate_message_template


@fixture(scope="module")
def private(slave):
    return slave.chat_with_alias


@fixture(scope="module")
def group(slave):
    return slave.group


@fixture(scope="module")
def group_member(slave):
    # Ensure the chat should have an alias
    for i in slave.group.members:
        if i.alias:
            return i
    return slave.group.members[0]


def build_dummy_message(chat: Chat, author: Chat) -> Message:
    message = Message()
    message.chat = chat
    message.author = author
    return message


def test_delivery_policy_defaults_to_normal(channel, private):
    msg = build_dummy_message(private, private)

    assert channel.slave_messages.delivery_policy(msg) is DeliveryPolicy.NORMAL


def test_delivery_message_type_distinguishes_public_account_and_finder():
    public = SimpleNamespace(vendor_specific={"is_mp": True})
    finder = SimpleNamespace(
        type=MsgType.Video,
        chat=SimpleNamespace(vendor_specific={}),
        vendor_specific={
            "wx_xml": "<msg><appmsg><finderFeed><objectId>1</objectId></finderFeed></appmsg></msg>",
            "comwechat_info": {"type": 49},
        },
    )
    article = SimpleNamespace(
        type=MsgType.Link,
        chat=public,
        vendor_specific={},
    )
    image = SimpleNamespace(
        type=MsgType.Image,
        chat=SimpleNamespace(vendor_specific={}),
        vendor_specific={},
    )

    assert SlaveMessageProcessor.delivery_message_type(article) == "public_account"
    assert SlaveMessageProcessor.delivery_message_type(finder) == "finder"
    assert SlaveMessageProcessor.delivery_message_type(image) == "image"


def test_filtered_delivery_stops_before_destination_lookup(channel, private, monkeypatch):
    msg = build_dummy_message(private, private)
    msg.uid = "filtered-message"
    monkeypatch.setattr(channel.delivery_policy_store, "get", lambda _: DeliveryPolicy.FILTERED)
    called = False

    def unexpected_destination(_):
        nonlocal called
        called = True

    monkeypatch.setattr(channel.slave_messages, "get_slave_msg_dest", unexpected_destination)

    assert channel.slave_messages.send_message(msg) is msg
    assert not called


def test_silent_delivery_overrides_normal_notification(channel, private, monkeypatch):
    msg = build_dummy_message(private, private)
    monkeypatch.setattr(channel.delivery_policy_store, "get", lambda _: DeliveryPolicy.SILENT)
    monkeypatch.setattr(channel.slave_messages, "get_slave_msg_dest", lambda _: ("", (1, None)))
    monkeypatch.setattr(channel.slave_messages, "is_silent", lambda _: False)
    captured = {}
    monkeypatch.setattr(channel.slave_messages, "dispatch_message",
                        lambda **kwargs: captured.update(kwargs))
    channel.wechat_read_ui.mark_message_read = Mock()

    channel.slave_messages.send_message(msg)

    assert captured["silent"] is True
    channel.wechat_read_ui.mark_message_read.assert_called_once_with(msg)


def test_cleanup_same_day_offline_notices_deletes_only_matching_logs():
    deleted = []
    removed = []

    class FakeDatabase:
        def get_msg_logs_by_text(self, text, since, origin_prefix):
            assert text == "检测到微信未登录，请发送 /login 获取登录二维码，或发送 /wechat 打开微信管理"
            assert origin_prefix == "honus.comwechat "
            return [SimpleNamespace(master_msg_id="123.10"), SimpleNamespace(master_msg_id="123.11")]

        def delete_msg_log(self, master_msg_id):
            removed.append(master_msg_id)

    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    processor.db = FakeDatabase()
    processor.bot = SimpleNamespace(
        delete_message=lambda chat_id, message_id: deleted.append((chat_id, message_id)),
    )

    count = processor.cleanup_same_day_offline_notices()

    assert count == 2
    assert deleted == [(123, 10), (123, 11)]
    assert removed == ["123.10", "123.11"]


def test_slave_message_generate_common_private(generate_message_template, private):
    message = build_dummy_message(private, private)
    header = generate_message_template(message, False)
    assert private.name in header
    assert private.alias in header
    assert private.channel_emoji in header
    assert Emoji.USER in header


def test_slave_message_generate_common_private_self(generate_message_template, private):
    message = build_dummy_message(private, private.self)
    header = generate_message_template(message, False)
    assert private.name in header
    assert private.alias in header
    assert private.channel_emoji in header
    assert private.self.name in header
    assert Emoji.USER in header


def test_slave_message_generate_common_linked(generate_message_template, private):
    message = build_dummy_message(private, private)
    header = generate_message_template(message, True)
    assert not header


def test_slave_message_generate_common_linked_self(generate_message_template, private):
    message = build_dummy_message(private, private.self)
    header = generate_message_template(message, True)
    assert private.name not in header
    assert private.alias not in header
    assert private.channel_emoji not in header
    assert private.self.name in header
    assert Emoji.USER not in header


def test_slave_message_generate_group_private(generate_message_template, group, group_member):
    message = build_dummy_message(group, group_member)
    header = generate_message_template(message, False)
    assert group.name in header
    assert group.alias in header
    assert group.channel_emoji in header
    assert group_member.name in header
    assert group_member.alias in header
    assert Emoji.GROUP in header


def test_slave_message_generate_group_private_self(generate_message_template, group):
    message = build_dummy_message(group, group.self)
    header = generate_message_template(message, False)
    assert group.name in header
    assert group.alias in header
    assert group.channel_emoji in header
    assert group.self.name in header
    assert Emoji.GROUP in header


def test_slave_message_generate_group_linked(generate_message_template, group, group_member):
    message = build_dummy_message(group, group_member)
    header = generate_message_template(message, True)
    assert group.name not in header
    assert group.alias not in header
    assert group.channel_emoji not in header
    assert Emoji.GROUP not in header
    assert group_member.name in header
    assert group_member.alias in header


def test_slave_message_generate_group_linked_self(generate_message_template, group):
    message = build_dummy_message(group, group.self)
    header = generate_message_template(message, True)
    assert group.name not in header
    assert group.alias not in header
    assert group.channel_emoji not in header
    assert Emoji.GROUP not in header
    assert group.self.name in header


@fixture(scope="module")
def build_inline_keyboard(channel):
    return channel.slave_messages.build_chat_info_inline_keyboard


def keyboard_to_sequence(markup: InlineKeyboardMarkup) -> str:
    x = []
    for row in markup.inline_keyboard:
        x.append(f"[{', '.join(button.text for button in row)}]")
    return f"[{', '.join(x)}]"


def test_build_inline_keyboard_empty(build_inline_keyboard, private):
    msg = build_dummy_message(private, private)
    keyboard = build_inline_keyboard(msg, "", "", None)
    seq = keyboard_to_sequence(keyboard)
    assert seq == '[]'


def test_build_inline_keyboard_full(build_inline_keyboard, private):
    msg = build_dummy_message(private, private)
    msg.text = "__text__"
    keyboard = build_inline_keyboard(msg, "__template__", "__reactions__", None)
    seq = keyboard_to_sequence(keyboard)
    assert "__text__" in seq
    assert "__template__" in seq
    assert "__reactions__" in seq


def test_build_inline_keyboard_existing_buttons(build_inline_keyboard, private):
    msg = build_dummy_message(private, private)
    msg.text = "__text__"
    markup = InlineKeyboardMarkup.from_row([
        InlineKeyboardButton("__button_a__"),
        InlineKeyboardButton("__button_b__"),
    ])
    keyboard = build_inline_keyboard(msg, "__template__", "__reactions__", markup)
    seq = keyboard_to_sequence(keyboard)
    assert "__button_a__" in seq
    assert "__button_b__" in seq
    assert "__text__" in seq
    assert "__template__" in seq
    assert "__reactions__" in seq


def test_format_text_message_template_shows_name_by_default(channel):
    processor = channel.slave_messages
    author = SimpleNamespace(alias="Alice", name="A", long_name="Alice (A)")

    result = processor.format_text_message_template("Alice (A):", author)

    assert result == "<b>Alice (A):</b>"


def test_format_text_message_template_can_hide_name_with_spoiler(channel):
    processor = channel.slave_messages
    author = SimpleNamespace(alias="Alice", name="A", long_name="Alice (A)")
    processor.channel.author_name_spoiler_store.set_enabled(True)
    try:
        result = processor.format_text_message_template("Alice (A):", author)
    finally:
        processor.channel.author_name_spoiler_store.set_enabled(False)

    assert result == "<b>Alice (<tg-spoiler>A</tg-spoiler>):</b>"


def test_format_text_message_template_blockquote(channel):
    processor = channel.slave_messages
    original_flag = processor.flag
    processor.flag = lambda key: "blockquote" if key == "author_format" else original_flag(key)
    try:
        result = processor.format_text_message_template("Alice:", None)
    finally:
        processor.flag = original_flag

    assert result == "<blockquote>Alice:</blockquote>"


def test_comwechat_original_image_is_forced_to_document():
    message = SimpleNamespace(
        vendor_specific={
            "comwechat_info": {
                "force_send_as_file": True,
            },
        },
    )

    assert SlaveMessageProcessor.force_image_document(message)


def test_normal_image_is_not_forced_to_document():
    message = SimpleNamespace(vendor_specific={})

    assert not SlaveMessageProcessor.force_image_document(message)
