import json
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler


LOGGER = logging.getLogger(__name__)


class AuthorNameSpoilerStore:
    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.enabled = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != self.VERSION:
                raise ValueError("unsupported author name spoiler format")
            self.enabled = bool(data.get("enabled", False))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.exception("Unable to load author name spoiler file: %s", self.path)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=str(self.path.parent),
                    prefix=f".{self.path.name}.", delete=False) as temp_file:
                json.dump(
                    {"version": self.VERSION, "enabled": self.enabled},
                    temp_file,
                    ensure_ascii=False,
                    indent=2,
                )
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_path = Path(temp_file.name)
            os.replace(str(temp_path), str(self.path))
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()


def panel_text(enabled: bool) -> str:
    return (
        "群成员姓名隐藏\n\n"
        f"状态：{'已开启' if enabled else '已关闭'}\n"
        "开启后，Telegram 群聊中的“昵称 (微信姓名)”会折叠微信姓名。\n"
        "关闭后按原样显示，不影响微信端和消息内容。"
    )


def panel_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "关闭姓名隐藏" if enabled else "开启姓名隐藏",
                callback_data=f"namespoiler:set:{'off' if enabled else 'on'}",
            ),
            InlineKeyboardButton("关闭页面", callback_data="namespoiler:close"),
        ]
    ])


class AuthorNameSpoilerUI:
    def __init__(self, channel):
        self.channel = channel
        dispatcher = channel.bot_manager.dispatcher
        dispatcher.add_handler(CommandHandler(["namespoiler", "spoiler"], self.show))
        dispatcher.add_handler(
            CallbackQueryHandler(self.callback, pattern=r"^namespoiler:")
        )

    def is_admin(self, update: Update) -> bool:
        return bool(
            update.effective_user
            and update.effective_user.id in self.channel.config["admins"]
        )

    def render(self, update: Update) -> None:
        store = self.channel.author_name_spoiler_store
        text = panel_text(store.enabled)
        markup = panel_keyboard(store.enabled)
        if update.callback_query:
            update.callback_query.edit_message_text(text, reply_markup=markup)
        else:
            update.effective_message.reply_text(text, reply_markup=markup)

    def show(self, update: Update, context: CallbackContext) -> None:
        if self.is_admin(update):
            self.render(update)

    def callback(self, update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        if not query:
            return
        if not self.is_admin(update):
            query.answer("无权执行", show_alert=True)
            return

        action = query.data or ""
        if action == "namespoiler:close":
            query.answer()
            query.message.delete()
            return
        if action == "namespoiler:set:on":
            self.channel.author_name_spoiler_store.set_enabled(True)
        elif action == "namespoiler:set:off":
            self.channel.author_name_spoiler_store.set_enabled(False)
        else:
            query.answer("无效操作", show_alert=True)
            return

        query.answer("设置已更新")
        self.render(update)
