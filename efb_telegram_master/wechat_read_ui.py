import os
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from ehforwarderbot import coordinator
from ehforwarderbot.chat import SelfChatMember

from . import utils


READ_CALLBACK = "wechatread:mark"
COMWECHAT_CHANNEL_ID = "honus.comwechat"


def append_read_button(
    markup: Optional[InlineKeyboardMarkup],
) -> InlineKeyboardMarkup:
    rows = [list(row) for row in getattr(markup, "inline_keyboard", [])]
    if not any(
        getattr(button, "callback_data", None) == READ_CALLBACK
        for row in rows
        for button in row
    ):
        rows.append([
            InlineKeyboardButton("标记微信已读", callback_data=READ_CALLBACK)
        ])
    return InlineKeyboardMarkup(rows)


def remove_read_button(
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
    def __init__(self, channel):
        self.channel = channel
        self.logger = logging.getLogger(__name__)
        self.button_enabled = os.getenv(
            "EFB_WECHAT_READ_BUTTON_ENABLED", "true"
        ).strip().lower() not in {"0", "false", "no", "off"}
        self.reply_enabled = os.getenv(
            "EFB_WECHAT_READ_ON_REPLY", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}

    def should_offer(self, msg) -> bool:
        return (
            self.button_enabled
            and getattr(getattr(msg, "chat", None), "module_id", None)
            == COMWECHAT_CHANNEL_ID
            and not isinstance(getattr(msg, "author", None), SelfChatMember)
        )

    def add_button(
        self,
        markup: Optional[InlineKeyboardMarkup],
    ) -> InlineKeyboardMarkup:
        return append_read_button(markup)

    def summary(self) -> str:
        return (
            f"按钮{'开启' if self.button_enabled else '关闭'}｜"
            f"回复自动已读{'开启' if self.reply_enabled else '关闭'}"
        )

    def mark_destination_read(self, destination: str) -> bool:
        if not self.reply_enabled:
            return False
        try:
            module_id, chat_uid, _ = utils.chat_id_str_to_id(destination)
            if module_id != COMWECHAT_CHANNEL_ID:
                return False
            slave = coordinator.get_module_by_id(module_id)
            mark_read = getattr(slave, "mark_wechat_read", None)
            if not callable(mark_read):
                return False
            mark_read(chat_uid)
            return True
        except Exception:
            self.logger.warning(
                "failed to mark replied WeChat conversation as read",
                exc_info=True,
            )
            return False

    def callback(self, update: Update, _context) -> None:
        query = update.callback_query
        if not query:
            return
        if (
            not update.effective_user
            or update.effective_user.id not in self.channel.config["admins"]
        ):
            query.answer("仅管理员可操作", show_alert=True)
            return
        try:
            master_msg_id = utils.message_id_to_str(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
            record = self.channel.db.get_msg_log(master_msg_id=master_msg_id)
            if not record:
                raise LookupError("message mapping missing")
            module_id, chat_uid, _ = utils.chat_id_str_to_id(
                record.slave_origin_uid
            )
            if module_id != COMWECHAT_CHANNEL_ID:
                raise LookupError("not a ComWechat message")
            slave = coordinator.get_module_by_id(module_id)
            mark_read = getattr(slave, "mark_wechat_read", None)
            if not callable(mark_read):
                raise RuntimeError("mark-as-read unavailable")
            mark_read(chat_uid)
            query.edit_message_reply_markup(
                reply_markup=remove_read_button(query.message.reply_markup)
            )
            query.answer("微信会话已标记为已读")
        except Exception:
            self.logger.warning("failed to mark WeChat conversation as read", exc_info=True)
            query.answer("标记失败，请稍后重试", show_alert=True)
