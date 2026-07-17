import logging
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from . import utils
from .delivery_policy import DeliveryPolicy


POLICY_LABELS = {
    DeliveryPolicy.NORMAL: "正常接收",
    DeliveryPolicy.SILENT: "静默接收",
    DeliveryPolicy.FILTERED: "完全过滤",
}


def parse_filter_action(data: str) -> Optional[Tuple[str, str]]:
    if not data.startswith("filter:"):
        return None
    parts = data.split(":", 2)
    return parts[1], parts[2] if len(parts) == 3 else ""


def build_policy_keyboard(current: DeliveryPolicy) -> InlineKeyboardMarkup:
    buttons = []
    for policy in DeliveryPolicy:
        label = POLICY_LABELS[policy]
        if policy is current:
            label = f"✓ {label}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"filter:set:{policy.value}"))
    return InlineKeyboardMarkup([
        buttons,
        [InlineKeyboardButton("恢复默认", callback_data="filter:reset")],
        [InlineKeyboardButton("返回", callback_data="filter:back"),
         InlineKeyboardButton("关闭", callback_data="filter:close")],
    ])


def build_list_navigation(offset: int, page_size: int, total: int) -> InlineKeyboardMarkup:
    row = []
    if offset > 0:
        row.append(InlineKeyboardButton("上一页", callback_data=f"filter:page:{max(0, offset - page_size)}"))
    if offset + page_size < total:
        row.append(InlineKeyboardButton("下一页", callback_data=f"filter:page:{offset + page_size}"))
    rows = [row] if row else []
    rows.append([InlineKeyboardButton("关闭", callback_data="filter:close")])
    return InlineKeyboardMarkup(rows)


class DeliveryPolicyUI:
    PAGE_SIZE = 10

    def __init__(self, channel):
        self.channel = channel
        self.store = channel.delivery_policy_store
        self.logger = logging.getLogger(__name__)
        self.sessions: Dict[Tuple[int, int], dict] = {}

    def _is_admin(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.channel.config["admins"])

    def _all_chats(self, pattern: str = "") -> List:
        chats = list(self.channel.chat_manager.all_chats)
        if pattern:
            needle = pattern.casefold()
            chats = [chat for chat in chats if needle in chat.long_name.casefold()]
        return sorted(chats, key=lambda chat: chat.last_message_time, reverse=True)

    def command(self, update: Update, context: CallbackContext):
        if not self._is_admin(update):
            update.effective_message.reply_text("只有管理员可以修改会话接收设置。")
            return
        pattern = " ".join(context.args).strip()
        chats = self._all_chats(pattern)
        sent = update.effective_message.reply_text("正在载入会话…")
        key = (sent.chat_id, sent.message_id)
        self.sessions[key] = {"chats": chats, "offset": 0, "selected": None}
        self._render_list(sent.chat_id, sent.message_id)

    def _render_list(self, chat_id: int, message_id: int):
        session = self.sessions[(chat_id, message_id)]
        chats = session["chats"]
        offset = session["offset"]
        rows = []
        for index in range(offset, min(offset + self.PAGE_SIZE, len(chats))):
            chat = chats[index]
            policy = self.store.get(utils.chat_id_to_str(chat=chat))
            rows.append([InlineKeyboardButton(
                f"{chat.channel_emoji}{chat.chat_type_emoji} {chat.long_name} · {POLICY_LABELS[policy]}",
                callback_data=f"filter:chat:{index}")])
        navigation = build_list_navigation(offset, self.PAGE_SIZE, len(chats)).inline_keyboard
        rows.extend(navigation)
        text = "会话接收设置\n请选择微信会话。"
        if not chats:
            text += "\n\n没有找到匹配的会话。"
        self.channel.bot_manager.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=InlineKeyboardMarkup(rows))

    def _render_detail(self, query, chat):
        chat_key = utils.chat_id_to_str(chat=chat)
        policy = self.store.get(chat_key)
        text = (f"会话接收设置\n\n会话：{chat.long_name}\n"
                f"类型：{chat.chat_type_emoji}\n当前：{POLICY_LABELS[policy]}\n\n"
                "正常接收：转发并通知\n"
                "静默接收：转发但不弹通知\n"
                "完全过滤：不转发，微信原消息保留")
        query.edit_message_text(text=text, reply_markup=build_policy_keyboard(policy))

    def callback(self, update: Update, context: CallbackContext):
        query = update.callback_query
        if not query or not query.data or not query.message:
            return
        if not self._is_admin(update):
            query.answer("只有管理员可以修改。", show_alert=True)
            return
        action = parse_filter_action(query.data)
        if action is None:
            return
        name, value = action
        key = (query.message.chat_id, query.message.message_id)
        if name == "close":
            query.answer()
            self.sessions.pop(key, None)
            query.message.delete()
            return
        session = self.sessions.get(key)
        if session is None:
            query.answer("设置页面已失效，请重新发送 /filter。", show_alert=True)
            return
        query.answer()
        if name == "page":
            session["offset"] = max(0, int(value))
            self._render_list(*key)
            return
        if name == "back":
            session["selected"] = None
            self._render_list(*key)
            return
        if name == "chat":
            index = int(value)
            if index < 0 or index >= len(session["chats"]):
                return
            session["selected"] = index
            self._render_detail(query, session["chats"][index])
            return
        selected = session.get("selected")
        if selected is None or selected >= len(session["chats"]):
            return
        chat = session["chats"][selected]
        chat_key = utils.chat_id_to_str(chat=chat)
        if name == "reset":
            self.store.reset(chat_key)
        elif name == "set":
            self.store.set(chat_key, DeliveryPolicy(value), name=chat.long_name,
                           chat_type=chat.__class__.__name__)
        self._render_detail(query, chat)
