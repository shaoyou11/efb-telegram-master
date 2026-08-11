from types import SimpleNamespace
from unittest.mock import Mock

from telegram.error import RetryAfter, TimedOut

from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.master_message import MasterMessageProcessor


def build_message(file_name=None):
    document = None
    if file_name is not None:
        document = SimpleNamespace(file_name=file_name)
    return SimpleNamespace(document=document)


def test_tg_image_document_matches_only_generated_png_names():
    assert MasterMessageProcessor.is_tg_image_document(
        build_message("tg_image_1999621452.png")
    ) is True
    assert MasterMessageProcessor.is_tg_image_document(
        build_message("photo.png")
    ) is False
    assert MasterMessageProcessor.is_tg_image_document(
        build_message("tg_image_1999621452.jpg")
    ) is False


def test_retry_decorator_honors_retry_after(monkeypatch):
    calls = Mock(side_effect=[RetryAfter(3), "ok"])
    sleep = Mock()
    monkeypatch.setattr("efb_telegram_master.bot_manager.time.sleep", sleep)
    TelegramBotManager.Decorators.enable_retry = True
    try:
        wrapped = TelegramBotManager.Decorators.retry_on_timeout(calls)
        assert wrapped() == "ok"
    finally:
        TelegramBotManager.Decorators.enable_retry = False

    assert calls.call_count == 2
    sleep.assert_called_once_with(3)


def test_retry_decorator_uses_exponential_timeout_backoff(monkeypatch):
    calls = Mock(side_effect=[TimedOut(), TimedOut(), "ok"])
    sleep = Mock()
    monkeypatch.setattr("efb_telegram_master.bot_manager.time.sleep", sleep)
    TelegramBotManager.Decorators.enable_retry = True
    try:
        wrapped = TelegramBotManager.Decorators.retry_on_timeout(calls)
        assert wrapped() == "ok"
    finally:
        TelegramBotManager.Decorators.enable_retry = False

    assert sleep.call_args_list == [((1.0,),), ((2.0,),)]


def test_remote_telegram_file_id_is_not_treated_as_empty():
    manager = object.__new__(TelegramBotManager)
    manager.send_message = Mock()

    result = manager._detect_empty_file(
        "AgACAgQAAxkBAAIBremote-file-id",
        1,
        "caption",
        "",
        "",
    )

    assert result is None
    manager.send_message.assert_not_called()


def test_missing_absolute_local_file_is_treated_as_empty():
    manager = object.__new__(TelegramBotManager)
    manager.channel = SimpleNamespace(_=lambda text: text)
    manager.send_message = Mock(return_value="empty-warning")

    result = manager._detect_empty_file(
        "/definitely-missing/telegram-upload.png",
        1,
        "caption",
        "",
        "",
    )

    assert result == "empty-warning"
    manager.send_message.assert_called_once()
