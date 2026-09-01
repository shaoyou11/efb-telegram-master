import asyncio
import threading
import time
from datetime import timedelta
from unittest.mock import patch

from telegram import Bot, CallbackQuery, Chat, Message, Update, User
from telegram.ext import ApplicationBuilder, MessageHandler, filters

from efb_telegram_master.ptb22_runtime import (
    AsyncioRunner,
    DispatcherFacade,
    PTB22Runtime,
    SyncBotProxy,
    normalize_proxy_url,
    retry_after_seconds,
    set_conversation_state,
)


class FakeAsyncBot:
    async def get_me(self):
        return User(id=7, first_name="ETM", is_bot=True)

    async def send_message(self, chat_id, text, **kwargs):
        return chat_id, text, kwargs, threading.current_thread().name

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        return chat_id, message_id, text, kwargs, threading.current_thread().name


class FakeApplication:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


def test_retry_after_accepts_current_and_future_ptb_types():
    assert retry_after_seconds(2) == 2.0
    assert retry_after_seconds(timedelta(seconds=3)) == 3.0


def test_normalize_proxy_url_preserves_legacy_authentication():
    assert normalize_proxy_url({"proxy_url": "http://proxy.test:8080"}) == "http://proxy.test:8080"
    assert normalize_proxy_url({
        "proxy_url": "socks5://proxy.test:1080",
        "urllib3_proxy_kwargs": {"username": "user@example", "password": "p@ss"},
    }) == "socks5://user%40example:p%40ss@proxy.test:1080"
    assert normalize_proxy_url({
        "proxy_url": "http://existing:secret@proxy.test:8080",
        "username": "ignored",
        "password": "ignored",
    }) == "http://existing:secret@proxy.test:8080"


def test_set_conversation_state_uses_ptb22_storage():
    handler = type("Handler", (), {"_conversations": {}})()
    set_conversation_state(handler, (1, 2), 3)
    assert handler._conversations == {(1, 2): 3}


def test_sync_bot_proxy_runs_coroutine_on_runtime_loop():
    runner = AsyncioRunner(thread_name="ptb22-test-loop")
    runner.start()
    try:
        bot = SyncBotProxy(FakeAsyncBot(), runner)
        result = bot.send_message(123, "hello", disable_notification=True)
        assert result[:3] == (123, "hello", {"disable_notification": True})
        assert result[3] == "ptb22-test-loop"
    finally:
        runner.stop()


def test_dispatcher_facade_executes_sync_callback_off_event_loop():
    runner = AsyncioRunner(thread_name="ptb22-handler-loop")
    runner.start()
    application = FakeApplication()
    bot = SyncBotProxy(FakeAsyncBot(), runner)
    called = []

    def callback(update, context):
        called.append((update.effective_message.reply_text("ok"), threading.current_thread().name))

    dispatcher = DispatcherFacade(application, bot)
    handler = MessageHandler(filters.TEXT, callback)
    dispatcher.add_handler(handler)
    wrapped = application.handlers[0][0].callback

    message = Message(
        message_id=1,
        date=None,
        chat=Chat(id=123, type=Chat.PRIVATE),
        from_user=User(id=9, first_name="Admin", is_bot=False),
        text="ping",
    )
    update = Update(update_id=1, message=message)

    try:
        asyncio.run(wrapped(update, object()))
        assert called[0][0][0:2] == (123, "ok")
        assert called[0][0][2]["reply_markup"] is None
        assert called[0][1] != threading.current_thread().name
    finally:
        runner.stop()


def test_callback_query_edits_nested_message_without_event_loop_deadlock():
    runner = AsyncioRunner(thread_name="ptb22-callback-loop")
    runner.start()
    application = FakeApplication()
    bot = SyncBotProxy(FakeAsyncBot(), runner)
    called = []

    def callback(update, _context):
        called.append(update.callback_query.edit_message_text("updated"))

    dispatcher = DispatcherFacade(application, bot)
    handler = MessageHandler(filters.ALL, callback)
    dispatcher.add_handler(handler)
    wrapped = application.handlers[0][0].callback

    message = Message(
        message_id=2,
        date=None,
        chat=Chat(id=123, type=Chat.PRIVATE),
        from_user=User(id=7, first_name="ETM", is_bot=True),
        text="before",
    )
    update = Update(
        update_id=3,
        callback_query=CallbackQuery(
            id="callback-1",
            from_user=User(id=9, first_name="Admin", is_bot=False),
            chat_instance="instance-1",
            message=message,
        ),
    )

    try:
        asyncio.run(wrapped(update, object()))
        assert called[0][:3] == (123, 2, "updated")
        assert called[0][4] == "ptb22-callback-loop"
    finally:
        runner.stop()


def test_real_application_processes_update_without_network():
    sent = []

    async def fake_post(_bot, endpoint, data=None, **_kwargs):
        if endpoint == "getMe":
            return {"id": 7, "first_name": "ETM", "is_bot": True, "username": "offline_bot"}
        if endpoint == "sendMessage":
            sent.append((data["chat_id"], data["text"]))
            return {
                "message_id": 2,
                "date": int(time.time()),
                "chat": {"id": data["chat_id"], "type": "private"},
                "text": data["text"],
            }
        return True

    with patch.object(Bot, "_post", new=fake_post):
        runtime = PTB22Runtime(
            ApplicationBuilder().token(
                "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            ).build()
        )
        runtime.dispatcher.add_handler(
            MessageHandler(filters.TEXT, lambda update, _context: update.message.reply_text("pong"))
        )
        update = Update(
            update_id=2,
            message=Message(
                message_id=1,
                date=None,
                chat=Chat(id=123, type=Chat.PRIVATE),
                from_user=User(id=9, first_name="Admin", is_bot=False),
                text="ping",
            ),
        )
        try:
            runtime.runner.submit(runtime.application.process_update(update))
            assert sent == [(123, "pong")]
        finally:
            runtime.stop()
            runtime.stop()
