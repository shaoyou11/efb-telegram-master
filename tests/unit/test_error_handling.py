from types import SimpleNamespace

import efb_telegram_master as channel_module
from efb_telegram_master import TelegramChannel
from telegram.error import BadRequest


class FakeUpdate:
    callback_query = object()


class FakeLogger:
    def __init__(self):
        self.errors = []
        self.debugs = []

    def error(self, *args, **kwargs):
        self.errors.append((args, kwargs))

    def debug(self, *args, **kwargs):
        self.debugs.append((args, kwargs))


def test_message_not_modified_is_silent_and_does_not_notify_admin(monkeypatch):
    monkeypatch.setattr(channel_module, "Update", FakeUpdate)
    logger = FakeLogger()
    notifications = []
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.logger = logger
    channel.bot_manager = SimpleNamespace(
        send_message=lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    channel.error(
        FakeUpdate(),
        SimpleNamespace(error=BadRequest("Message is not modified")),
    )

    assert logger.errors == []
    assert logger.debugs
    assert notifications == []
