import string
import random
from types import SimpleNamespace
from typing import IO, Iterator, BinaryIO
from unittest.mock import patch

import pytest
from telegram.error import BadRequest
from telegram import InputMediaDocument

from efb_telegram_master.bot_manager import TelegramBotManager


def test_invalid_quote_retries_as_plain_reply():
    calls = []

    def send(_manager, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BadRequest("Quote_text_invalid")
        return "sent"

    wrapped = TelegramBotManager.Decorators.retry_on_invalid_quote(send)
    manager = SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    result = wrapped(
        manager,
        chat_id=1,
        api_kwargs={
            "reply_parameters": {"message_id": 42, "quote": "stale quote"},
            "show_caption_above_media": True,
        },
    )

    assert result == "sent"
    assert calls[1]["reply_to_message_id"] == 42
    assert calls[1]["api_kwargs"] == {"show_caption_above_media": True}
    assert "reply_parameters" not in calls[1]["api_kwargs"]


def test_invalid_quote_drops_reply_when_target_is_missing():
    calls = []

    def send(_manager, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BadRequest("Quote_text_invalid")
        if len(calls) == 2:
            raise BadRequest("message to reply not found")
        return "sent"

    wrapped = TelegramBotManager.Decorators.retry_on_invalid_quote(send)
    manager = SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
    )

    result = wrapped(
        manager,
        chat_id=1,
        api_kwargs={"reply_parameters": {"message_id": 42, "quote": "stale quote"}},
    )

    assert result == "sent"
    assert len(calls) == 3
    assert "reply_to_message_id" not in calls[2]
    assert "api_kwargs" not in calls[2]


def test_text_prefix_suffix(channel, bot_admin):
    message = channel.bot_manager.send_message(bot_admin, 'Message', prefix='Prefix', suffix='Suffix')
    assert message.text == 'Prefix\nMessage\nSuffix'

    edited = channel.bot_manager.edit_message_text(
        text="Edited text", prefix="Edited prefix", suffix="Edited suffix",
        chat_id=message.chat_id, message_id=message.message_id)
    assert edited.chat_id == message.chat_id
    assert edited.message_id == message.message_id
    assert edited.text == "Edited prefix\nEdited text\nEdited suffix"


@pytest.fixture(scope='function')
def image() -> Iterator[BinaryIO]:
    f = open('tests/mocks/image.png', 'rb')
    yield f
    f.close()


def test_caption_prefix_suffix(channel, bot_admin, image):
    message = channel.bot_manager.send_photo(bot_admin, image, caption='Message', prefix='Prefix', suffix='Suffix')
    assert message.caption == 'Prefix\nMessage\nSuffix'

    edited = channel.bot_manager.edit_message_caption(
        caption="Edited text", prefix="Edited prefix", suffix="Edited suffix",
        chat_id=message.chat_id, message_id=message.message_id)
    assert edited.chat_id == message.chat_id
    assert edited.message_id == message.message_id
    assert edited.caption == "Edited prefix\nEdited text\nEdited suffix"


def test_message_truncation(channel, bot_admin):
    msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
    with patch('telegram.Bot.send_document') as mock_send_document:
        message = channel.bot_manager.send_message(bot_admin, msg_body, prefix='Prefix')
        assert message.text.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')

        # Edit message text
        msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
        edited = channel.bot_manager.edit_message_text(
            text=msg_body, prefix='Prefix',
            chat_id=message.chat_id, message_id=message.message_id
        )
        assert edited.text.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')


def test_caption_truncation(channel, bot_admin, image):
    msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
    with patch('telegram.Bot.send_document') as mock_send_document:
        message = channel.bot_manager.send_photo(bot_admin, image, caption=msg_body, prefix='Prefix')
        assert message.caption.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')

        # Edit message text
        msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
        edited = channel.bot_manager.edit_message_caption(
            caption=msg_body, prefix='Prefix',
            chat_id=message.chat_id, message_id=message.message_id
        )
        assert edited.caption.startswith('Prefix\n' + msg_body[:50])


def test_malformed_markdown_text(channel, bot_admin):
    channel.bot_manager.send_message(
        bot_admin,
        "*some _strange_ styling* with [an *incomplete* link](https://example.com/this.is.a.(link",
        parse_mode="markdown"
    )


def test_malformed_markdown_caption(channel, bot_admin, image):
    channel.bot_manager.send_photo(
        bot_admin,
        image,
        caption="*some _strange_ styling* with [an *incomplete* link](https://example.com/this.is.a.(link",
        parse_mode="markdown"
    )


def test_malformed_html_text(channel, bot_admin):
    channel.bot_manager.send_message(
        bot_admin,
        '<b>Bold and <i>italics</i> text</b> and <abbr title="unknown tag to Telegram">UTTT</abbr> and an <a href="https://example.com">incomplete link</a',
        parse_mode="html"
    )


def test_malformed_html_caption(channel, bot_admin, image):
    channel.bot_manager.send_photo(
        bot_admin,
        image,
        caption='<b>Bold and <i>italics</i> text</b> and <abbr title="unknown tag to Telegram">UTTT</abbr> and an <a href="https://example.com">incomplete link</a',
        parse_mode="html"
    )


def test_edit_message_media_sets_and_clears_parse_mode(channel, bot_admin):
    media = InputMediaDocument("file_id")
    with patch("telegram.Bot.edit_message_media") as mock_edit:
        channel.bot_manager.edit_message_media(
            chat_id=bot_admin,
            message_id=1,
            media=media,
            caption="<b>Alice:</b>",
            parse_mode="HTML",
        )
        sent_media = mock_edit.call_args.kwargs["media"]
        assert sent_media.caption == "<b>Alice:</b>"
        assert sent_media.parse_mode == "HTML"

        channel.bot_manager.edit_message_media(
            chat_id=bot_admin,
            message_id=1,
            media=media,
            caption="Alice:",
        )
        retried_media = mock_edit.call_args.kwargs["media"]
        assert retried_media.parse_mode is None
