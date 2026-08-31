import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardMarkup, Update

from ehforwarderbot import coordinator
from ehforwarderbot.chat import SelfChatMember


LOGGER = logging.getLogger(__name__)
READ_CALLBACK = "wechatread:mark"
COMWECHAT_CHANNEL_ID = "honus.comwechat"


def remove_legacy_read_button(
    markup: Optional[InlineKeyboardMarkup],
) -> Optional[InlineKeyboardMarkup]:
    rows = []
    for row in getattr(markup, "inline_keyboard", []):
        kept = [
            button for button in row
            if getattr(button, "callback_data", None) != READ_CALLBACK
        ]
        if kept:
            rows.append(kept)
    return InlineKeyboardMarkup(rows) if rows else None


class WechatReadUI:
    VERSION = 1

    def __init__(self, channel, state_path: Optional[Path] = None):
        self.channel = channel
        data_root = Path(os.getenv("EFB_DATA_ROOT", "/data"))
        self.state_path = Path(
            state_path
            or data_root / "operations" / "state" / "wechat-read-settings.json"
        )
        self.enabled = False
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
                raise ValueError("unsupported WeChat read settings")
            self.enabled = bool(payload.get("enabled", False))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.exception("Unable to load WeChat read settings: %s", self.state_path)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.state_path.parent),
                prefix=f".{self.state_path.name}.",
                delete=False,
            ) as handle:
                json.dump(
                    {"version": self.VERSION, "enabled": self.enabled},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(str(temporary), str(self.state_path))
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def summary(self) -> str:
        return "开启" if self.enabled else "关闭"

    def mark_message_read(self, msg) -> bool:
        if not self.enabled:
            return False
        chat = getattr(msg, "chat", None)
        if (
            getattr(chat, "module_id", None) != COMWECHAT_CHANNEL_ID
            or isinstance(getattr(msg, "author", None), SelfChatMember)
        ):
            return False
        try:
            slave = coordinator.get_module_by_id(COMWECHAT_CHANNEL_ID)
            mark_read = getattr(slave, "mark_wechat_read", None)
            if not callable(mark_read):
                raise RuntimeError("mark-as-read unavailable")
            mark_read(chat.uid)
            return True
        except Exception:
            LOGGER.warning(
                "failed to mark delivered WeChat conversation as read",
                exc_info=True,
            )
            return False

    def legacy_callback(self, update: Update, _context) -> None:
        query = update.callback_query
        if not query:
            return
        if (
            not update.effective_user
            or update.effective_user.id not in self.channel.config["admins"]
        ):
            query.answer("仅管理员可操作", show_alert=True)
            return
        query.edit_message_reply_markup(
            reply_markup=remove_legacy_read_button(query.message.reply_markup)
        )
        query.answer("已改为 /status 中的微信自动已读总开关", show_alert=True)
