import asyncio
import inspect
import threading
from concurrent.futures import Future
from typing import Any, Coroutine, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from telegram import TelegramObject, Update
from telegram.ext import Application, BaseHandler, ConversationHandler

_SYNC_OBJECT_CLASSES = {}


def retry_after_seconds(value: Any) -> float:
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    return float(value)


def normalize_proxy_url(config: Any) -> Optional[str]:
    """Convert legacy separate proxy credentials into a PTB 22 proxy URL."""
    if not isinstance(config, dict):
        return None
    proxy_url = config.get("proxy_url") or config.get("proxy")
    if not proxy_url:
        return None
    parsed = urlsplit(str(proxy_url))
    if parsed.username is not None:
        return str(proxy_url)

    legacy_auth = config.get("urllib3_proxy_kwargs") or {}
    username = config.get("username", legacy_auth.get("username"))
    password = config.get("password", legacy_auth.get("password"))
    if username is None or password is None:
        return str(proxy_url)

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    auth = f"{quote(str(username), safe='')}:{quote(str(password), safe='')}@"
    return urlunsplit((parsed.scheme, f"{auth}{hostname}{port}", parsed.path, parsed.query, parsed.fragment))


def set_conversation_state(handler: ConversationHandler, key: Any, value: Any) -> None:
    """Set a conversation state across the PTB 13 and PTB 22 attribute names."""
    conversations = getattr(handler, "conversations", None)
    if conversations is None:
        conversations = handler._conversations
    conversations[key] = value


def _set_bot_tree(value: Any, bot: Any, seen: Optional[set] = None) -> None:
    """Set one bot on a Telegram object and every nested Telegram object."""
    if seen is None:
        seen = set()
    value_id = id(value)
    if value_id in seen:
        return
    seen.add(value_id)

    if isinstance(value, TelegramObject):
        value.set_bot(bot)
        for cls in type(value).__mro__:
            for name in getattr(cls, "__slots__", ()):
                if name == "_bot":
                    continue
                try:
                    child = getattr(value, name)
                except (AttributeError, RuntimeError):
                    continue
                _set_bot_tree(child, bot, seen)
    elif isinstance(value, dict):
        for child in value.values():
            _set_bot_tree(child, bot, seen)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            _set_bot_tree(child, bot, seen)


def _sync_object_class(original_class):
    cached = _SYNC_OBJECT_CLASSES.get(original_class)
    if cached:
        return cached

    attributes = {"__slots__": ()}
    for name in dir(original_class):
        if name.startswith("_"):
            continue
        original = getattr(original_class, name, None)
        if not inspect.iscoroutinefunction(original):
            continue

        def sync_method(self, *args, __method=original, **kwargs):
            proxy = self.get_bot()

            if not isinstance(proxy, SyncBotProxy):
                return __method(self, *args, **kwargs)

            async def invoke():
                _set_bot_tree(self, proxy._async_bot)
                try:
                    return await __method(self, *args, **kwargs)
                finally:
                    _set_bot_tree(self, proxy)

            return proxy._runner.submit(invoke())

        sync_method.__name__ = name
        attributes[name] = sync_method
    sync_class = type(f"ETMSync{original_class.__name__}", (original_class,), attributes)
    _SYNC_OBJECT_CLASSES[original_class] = sync_class
    return sync_class


class AsyncioRunner:
    """Own an asyncio loop in a dedicated thread for PTB 22."""

    def __init__(self, thread_name: str = "ETM PTB 22 event loop"):
        self.thread_name = thread_name
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("PTB 22 event loop did not start")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        if not self.loop or not self.thread or not self.thread.is_alive():
            raise RuntimeError("PTB 22 event loop is not running")
        if threading.current_thread() is self.thread:
            raise RuntimeError("Synchronous PTB call from its own event loop would deadlock")
        future: Future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()

    def stop(self) -> None:
        if not self.loop or not self.thread:
            return
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
        self.loop = None
        self.thread = None
        self._ready.clear()


def _bind_sync_bot(value: Any, bot: "SyncBotProxy", seen: Optional[set] = None) -> Any:
    """Attach the synchronous proxy to PTB objects and their nested objects."""
    if seen is None:
        seen = set()
    value_id = id(value)
    if value_id in seen:
        return value
    seen.add(value_id)

    if isinstance(value, TelegramObject):
        original_class = type(value)
        if not original_class.__name__.startswith("ETMSync"):
            sync_class = _sync_object_class(original_class)
            if sync_class is not original_class:
                value.__class__ = sync_class
        value.set_bot(bot)
        for cls in type(value).__mro__:
            for name in getattr(cls, "__slots__", ()):
                if name == "_bot":
                    continue
                try:
                    child = getattr(value, name)
                except (AttributeError, RuntimeError):
                    continue
                _bind_sync_bot(child, bot, seen)
    elif isinstance(value, dict):
        for child in value.values():
            _bind_sync_bot(child, bot, seen)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            _bind_sync_bot(child, bot, seen)
    return value


class SyncBotProxy:
    """Expose PTB 22 coroutine methods through the existing synchronous ETM API."""

    def __init__(self, bot: Any, runner: AsyncioRunner):
        self._async_bot = bot
        self._runner = runner

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._async_bot, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            result = target(*args, **kwargs)
            if inspect.isawaitable(result):
                result = self._runner.submit(result)
            return _bind_sync_bot(result, self)

        return call


def _wrap_handler(handler: BaseHandler, bot: SyncBotProxy) -> BaseHandler:
    if isinstance(handler, ConversationHandler):
        for nested in handler.entry_points:
            _wrap_handler(nested, bot)
        for handlers in handler.states.values():
            for nested in handlers:
                _wrap_handler(nested, bot)
        for nested in handler.fallbacks:
            _wrap_handler(nested, bot)
        return handler

    callback = handler.callback
    if inspect.iscoroutinefunction(callback):
        return handler
    if getattr(callback, "_etm_ptb22_wrapped", False):
        return handler

    async def async_callback(update: Update, context: Any):
        _bind_sync_bot(update, bot)
        return await asyncio.to_thread(callback, update, context)

    async_callback._etm_ptb22_wrapped = True
    handler.callback = async_callback
    return handler


class DispatcherFacade:
    """Keep the PTB 13 dispatcher surface while using a PTB 22 Application."""

    def __init__(self, application: Application, bot: SyncBotProxy):
        self.application = application
        self.bot = bot

    def add_handler(self, handler: BaseHandler, group: int = 0) -> None:
        self.application.add_handler(_wrap_handler(handler, self.bot), group=group)

    def add_error_handler(self, callback) -> None:
        async def async_error_handler(update: object, context: Any):
            if isinstance(update, Update):
                _bind_sync_bot(update, self.bot)
            return await asyncio.to_thread(callback, update, context)

        self.application.add_error_handler(async_error_handler)


class PTB22Runtime:
    """Lifecycle wrapper used by TelegramBotManager."""

    def __init__(self, application: Application):
        self.application = application
        self.runner = AsyncioRunner()
        self._lifecycle_lock = threading.Lock()
        self._stopped = False
        self.runner.start()
        try:
            self.runner.submit(self.application.initialize())
        except Exception:
            self.runner.stop()
            raise
        self.bot = SyncBotProxy(self.application.bot, self.runner)
        self.dispatcher = DispatcherFacade(self.application, self.bot)

    async def _start_polling(self, timeout: int, drop_pending_updates: bool) -> None:
        await self.application.updater.start_polling(
            timeout=timeout,
            drop_pending_updates=drop_pending_updates,
        )
        try:
            await self.application.start()
        except Exception:
            await self.application.updater.stop()
            raise

    def start_polling(self, timeout: int = 10, drop_pending_updates: bool = False) -> None:
        self.runner.submit(self._start_polling(timeout, drop_pending_updates))

    async def _start_webhook(self, kwargs) -> None:
        await self.application.updater.start_webhook(**kwargs)
        try:
            await self.application.start()
        except Exception:
            await self.application.updater.stop()
            raise

    def start_webhook(self, **kwargs) -> None:
        self.runner.submit(self._start_webhook(kwargs))

    async def _stop(self) -> None:
        if self.application.updater and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self.runner.submit(self._stop())
        finally:
            self.runner.stop()
