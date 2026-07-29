import logging
from typing import Dict, List, Optional, Tuple

from ehforwarderbot import coordinator
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
        if not chats:
            for slave in coordinator.slaves.values():
                try:
                    for chat in slave.get_chats():
                        self.channel.chat_manager.update_chat_obj(chat, full_update=True)
                except Exception:
                    self.logger.exception("Unable to refresh chats from %s", slave.channel_id)
            chats = list(self.channel.chat_manager.all_chats)
        if pattern:
            needle = pattern.casefold()
            chats = [chat for chat in chats if needle in chat.long_name.casefold()]
        return sorted(chats, key=lambda chat: chat.last_message_time, reverse=True)

    def _view_chats(self, chats: List, view: str) -> List:
        if view == "silent":
            return [chat for chat in chats if self.store.get(utils.chat_id_to_str(chat=chat)) is DeliveryPolicy.SILENT]
        if view == "filtered":
            return [chat for chat in chats if self.store.get(utils.chat_id_to_str(chat=chat)) is DeliveryPolicy.FILTERED]
        if view == "mp":
            return [chat for chat in chats if getattr(chat, "vendor_specific", {}).get("is_mp")]
        if view == "group":
            return [chat for chat in chats if "Group" in chat.__class__.__name__]
        if view == "contact":
            return [
                chat for chat in chats
                if not getattr(chat, "vendor_specific", {}).get("is_mp")
                and "Group" not in chat.__class__.__name__
            ]
        return chats

    def _context_chat(self, update: Update):
        if not update.effective_chat or not update.effective_message:
            return None
        if update.effective_chat.type == "private":
            return None

        links = []
        thread_id = update.effective_message.message_thread_id
        if update.effective_chat.is_forum and thread_id:
            linked = self.channel.db.get_topic_slave(
                topic_chat_id=update.effective_chat.id,
                message_thread_id=thread_id,
            )
            if linked:
                links = [linked]
        else:
            master_uid = utils.chat_id_to_str(
                channel_id=self.channel.channel_id,
                chat_uid=str(update.effective_chat.id),
            )
            links = self.channel.db.get_chat_assoc(master_uid=master_uid)

        if len(links) != 1:
            return None
        channel_id, chat_uid, _ = utils.chat_id_str_to_id(links[0])
        return self.channel.chat_manager.get_chat(channel_id, chat_uid)

    def command(self, update: Update, context: CallbackContext):
        if not self._is_admin(update):
            update.effective_message.reply_text("只有管理员可以修改会话接收设置。")
            return
        sent = update.effective_message.reply_text("正在载入会话…")
        pattern = " ".join(context.args).strip()
        current_chat = None if pattern else self._context_chat(update)
        matched_chats = [] if current_chat else self._all_chats(pattern)
        chats = [current_chat] if current_chat else (matched_chats if pattern else [])
        key = (sent.chat_id, sent.message_id)
        self.sessions[key] = {
            "all_chats": matched_chats,
            "chats": chats,
            "offset": 0,
            "selected": 0 if current_chat else None,
            "view": "detail" if current_chat else ("search" if pattern else "overview"),
        }
        if current_chat:
            self._render_detail_message(sent.chat_id, sent.message_id, current_chat)
        else:
            self._render_list(sent.chat_id, sent.message_id)

    def _render_list(self, chat_id: int, message_id: int):
        session = self.sessions[(chat_id, message_id)]
        chats = session["chats"]
        offset = session["offset"]
        view = session["view"]
        rows = []
        if view != "overview":
            for index in range(offset, min(offset + self.PAGE_SIZE, len(chats))):
                chat = chats[index]
                policy = self.store.get(utils.chat_id_to_str(chat=chat))
                rows.append([InlineKeyboardButton(
                    f"{chat.channel_emoji}{chat.chat_type_emoji} {chat.long_name} · {POLICY_LABELS[policy]}",
                    callback_data=f"filter:chat:{index}")])
        rows.insert(0, [
            InlineKeyboardButton("静默", callback_data="filter:view:silent"),
            InlineKeyboardButton("已过滤", callback_data="filter:view:filtered"),
        ])
        rows.insert(1, [
            InlineKeyboardButton("公众号", callback_data="filter:view:mp"),
            InlineKeyboardButton("群聊", callback_data="filter:view:group"),
            InlineKeyboardButton("联系人", callback_data="filter:view:contact"),
        ])
        quiet = self.store.quiet_hours()
        quiet_label = "关闭夜间静默" if quiet["enabled"] else "开启夜间静默 23:00-07:00"
        rows.insert(2, [InlineKeyboardButton(quiet_label, callback_data="filter:quiet:toggle")])
        if view == "mp" and chats:
            rows.insert(3, [
                InlineKeyboardButton("公众号全部静默", callback_data="filter:bulk:silent"),
                InlineKeyboardButton("公众号全部过滤", callback_data="filter:bulk:filtered"),
            ])
        if view != "overview":
            rows.append([InlineKeyboardButton("返回概览", callback_data="filter:view:overview")])
        rows.extend(build_list_navigation(offset, self.PAGE_SIZE, len(chats)).inline_keyboard)
        rules = self.store.list_rules()
        silent_count = sum(1 for rule in rules.values() if rule.get("policy") == "silent")
        filtered_count = sum(1 for rule in rules.values() if rule.get("policy") == "filtered")
        if view == "overview":
            text = ("会话接收设置\n\n"
                    f"静默：{silent_count} 个｜完全过滤：{filtered_count} 个\n\n"
                    "请选择分类，或发送 /filter 关键词搜索会话。")
        else:
            text = ("会话接收设置\n请选择微信会话。\n\n"
                    f"当前列表：{len(chats)} 个｜静默：{silent_count}｜过滤：{filtered_count}")
        if view != "overview" and not chats:
            text += "\n\n没有找到匹配的会话。"
        self.channel.bot_manager.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=InlineKeyboardMarkup(rows))

    def _detail_content(self, chat):
        chat_key = utils.chat_id_to_str(chat=chat)
        policy = self.store.get(chat_key)
        text = (f"会话接收设置\n\n会话：{chat.long_name}\n"
                f"类型：{chat.chat_type_emoji}\n当前：{POLICY_LABELS[policy]}\n\n"
                "正常接收：转发并通知\n"
                "静默接收：转发但不弹通知\n"
                "完全过滤：不转发，微信原消息保留")
        return text, build_policy_keyboard(policy)

    def _render_detail_message(self, chat_id: int, message_id: int, chat):
        text, markup = self._detail_content(chat)
        self.channel.bot_manager.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)

    def _render_detail(self, query, chat):
        text, markup = self._detail_content(chat)
        query.edit_message_text(text=text, reply_markup=markup)

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
        if name == "view":
            session["view"] = value
            session["chats"] = (
                [] if value == "overview"
                else self._view_chats(session["all_chats"], value)
            )
            session["offset"] = 0
            self._render_list(*key)
            return
        if name == "quiet":
            quiet = self.store.quiet_hours()
            self.store.set_quiet_hours("23:00", "07:00", not quiet["enabled"])
            self._render_list(*key)
            return
        if name == "bulk":
            if session.get("view") != "mp" or value not in {"silent", "filtered"}:
                return
            label = "静默接收" if value == "silent" else "完全过滤"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("确认", callback_data=f"filter:confirm:{value}"),
                InlineKeyboardButton("取消", callback_data="filter:backlist"),
            ], [InlineKeyboardButton("关闭", callback_data="filter:close")]])
            query.edit_message_text(
                f"批量设置确认\n\n将 {len(session['chats'])} 个公众号设置为“{label}”。\n"
                "只修改接收策略，不删除微信或 Telegram 消息。",
                reply_markup=markup,
            )
            return
        if name == "confirm":
            if session.get("view") != "mp" or value not in {"silent", "filtered"}:
                return
            policy = DeliveryPolicy(value)
            for chat in session["chats"]:
                self.store.set(utils.chat_id_to_str(chat=chat), policy,
                               name=chat.long_name, chat_type=chat.__class__.__name__)
            self._render_list(*key)
            return
        if name == "backlist":
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
