import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from .bridge_queue import BridgeQueueClient, BridgeQueueError, BridgeQueueSettings


URL = re.compile(r"https?://[^\s]+")
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s]+")
PAGE_SIZE = 5


def _safe_text(value: Any, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    text = URL.sub("[链接]", text)
    text = PATH.sub("[路径]", text)
    return text[:limit] or "暂无内容"


def _message_summary(item: Dict[str, Any]) -> str:
    message = item.get("message") if isinstance(item.get("message"), dict) else {}
    sender = _safe_text(message.get("sender") or message.get("wxid") or "未知联系人", 28)
    message_type = str(message.get("type") or "消息")
    type_names = {
        "1": "文字",
        "3": "图片",
        "34": "语音",
        "43": "视频",
        "47": "表情",
        "49": "分享",
    }
    kind = type_names.get(message_type, message_type)
    content = _safe_text(message.get("content"), 70)
    if message.get("filepath") or message.get("thumb_path"):
        content = "[附件]" if content == "暂无内容" else content + " [附件]"
    return f"{sender}｜{kind}｜{content}"


def _state_name(state: str) -> str:
    return {
        "staged": "暂存",
        "pending": "待投递",
        "inflight": "处理中",
        "dead": "死信",
    }.get(state, state or "未知")


def _page_slice(items: List[Dict[str, Any]], page: int) -> Tuple[List[Dict[str, Any]], int]:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, int(page)), total_pages - 1)
    start = page * PAGE_SIZE
    return items[start:start + PAGE_SIZE], total_pages


class BridgeQueueUI:
    def __init__(
        self,
        channel,
        client: Optional[BridgeQueueClient] = None,
        settings: Optional[BridgeQueueSettings] = None,
    ):
        self.channel = channel
        data_root = Path(os.getenv("EFB_DATA_ROOT", "/data"))
        self.settings = settings or BridgeQueueSettings(
            data_root / "operations" / "state" / "bridge-queue-settings.json"
        )
        self.client = client or BridgeQueueClient(
            os.getenv("COMWECHAT_BRIDGE_API_BASE", "http://comwechat:19088")
        )

    @staticmethod
    def home_text(snapshot: Dict[str, Any], enabled: bool) -> str:
        queue_size = int(snapshot.get("queue_size", 0) or 0)
        staged = int(snapshot.get("staged_size", 0) or 0)
        pending = int(snapshot.get("pending_size", 0) or 0)
        inflight = int(snapshot.get("inflight_size", 0) or 0)
        dead = int(snapshot.get("dead_letter_size", 0) or 0)
        discarded = int(snapshot.get("discarded_size", 0) or 0)
        return (
            "Bridge 队列管理\n\n"
            f"待处理：{queue_size}\n"
            f"  暂存 {staged}｜待投递 {pending}｜处理中 {inflight}\n"
            f"死信：{dead}\n"
            f"放弃记录：{discarded}\n\n"
            f"管理开关：{'开启' if enabled else '关闭'}\n"
            "说明：关闭时仍可查看，重试、重新投递和放弃操作会被拦截。"
        )

    @staticmethod
    def active_text(items: List[Dict[str, Any]], page: int = 0) -> str:
        page_items, total_pages = _page_slice(items, page)
        lines = [f"Bridge 活动队列（第 {min(page + 1, total_pages)}/{total_pages} 页）", ""]
        if not page_items:
            lines.append("当前没有活动消息。")
        for index, item in enumerate(page_items, start=page * PAGE_SIZE + 1):
            message_id = _safe_text(item.get("id"), 12)
            state = _state_name(str(item.get("state") or ""))
            lines.append(f"{index}. {message_id}｜{state}")
            lines.append(f"   {_message_summary(item)}")
            if item.get("last_error"):
                lines.append(f"   原因：{_safe_text(item.get('last_error'), 70)}")
        return "\n".join(lines)

    @staticmethod
    def dead_text(items: List[Dict[str, Any]], page: int = 0) -> str:
        page_items, total_pages = _page_slice(items, page)
        lines = [f"Bridge 死信队列（第 {min(page + 1, total_pages)}/{total_pages} 页）", ""]
        if not page_items:
            lines.append("当前没有死信。")
        for index, item in enumerate(page_items, start=page * PAGE_SIZE + 1):
            message_id = _safe_text(item.get("id"), 12)
            reason = _safe_text(item.get("last_error") or "未知原因", 80)
            lines.append(f"{index}. {message_id}｜尝试 {item.get('attempts', 0)} 次")
            lines.append(f"   原因：{reason}")
        return "\n".join(lines)

    def _allowed(self, update: Update) -> bool:
        return bool(
            update.effective_user
            and update.effective_user.id in self.channel.config["admins"]
        )

    @staticmethod
    def _home_markup(enabled: bool) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("活动队列", callback_data="bridgeq:active:0"),
                InlineKeyboardButton("死信队列", callback_data="bridgeq:dead:0"),
            ],
            [InlineKeyboardButton(
                f"管理开关：{'开启' if enabled else '关闭'}",
                callback_data="bridgeq:toggle",
            )],
            [
                InlineKeyboardButton("刷新", callback_data="bridgeq:home"),
                InlineKeyboardButton("隐藏", callback_data="bridgeq:close"),
            ],
        ])

    @staticmethod
    def _page_markup(
        kind: str,
        items: List[Dict[str, Any]],
        page: int,
        enabled: bool,
    ) -> InlineKeyboardMarkup:
        page_items, total_pages = _page_slice(items, page)
        rows = []
        for item in page_items:
            message_id = str(item.get("id") or "")
            state = str(item.get("state") or "")
            label = _safe_text(message_id, 10)
            if kind == "active" and state == "inflight":
                rows.append([InlineKeyboardButton(f"{label}：处理中", callback_data="bridgeq:noop")])
            elif enabled:
                if kind == "active":
                    rows.append([
                        InlineKeyboardButton("立即重试", callback_data=f"bridgeq:retry:{message_id}"),
                        InlineKeyboardButton("放弃", callback_data=f"bridgeq:discard:{message_id}"),
                    ])
                else:
                    rows.append([
                        InlineKeyboardButton("重新投递", callback_data=f"bridgeq:requeue:{message_id}"),
                        InlineKeyboardButton("放弃", callback_data=f"bridgeq:discard:{message_id}"),
                    ])
        if kind == "dead" and enabled:
            rows.append([
                InlineKeyboardButton("全部重新投递", callback_data="bridgeq:requeue-all"),
                InlineKeyboardButton("全部放弃", callback_data="bridgeq:discard-all"),
            ])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton("上一页", callback_data=f"bridgeq:{kind}:{page - 1}"))
        navigation.append(InlineKeyboardButton("返回首页", callback_data="bridgeq:home"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton("下一页", callback_data=f"bridgeq:{kind}:{page + 1}"))
        rows.append(navigation)
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _result_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("返回 Bridge 首页", callback_data="bridgeq:home")]])

    @staticmethod
    def _confirm_markup(action: str, message_id: str = "") -> InlineKeyboardMarkup:
        suffix = f":{message_id}" if message_id else ""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("确认", callback_data=f"bridgeq:{action}-confirm{suffix}"),
                InlineKeyboardButton("取消", callback_data="bridgeq:home"),
            ]
        ])

    def _edit(self, update: Update, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        if update.callback_query:
            update.callback_query.edit_message_text(text, reply_markup=markup)
        elif update.effective_message:
            update.effective_message.reply_text(text, reply_markup=markup)

    def _error(self, update: Update):
        self._edit(update, "Bridge 队列暂时无法读取，请稍后重试。", self._result_markup())

    def _forget_dead(self, message_id: str) -> None:
        guard = getattr(self.channel, "bridge_dead_letter_guard", None)
        forget = getattr(guard, "forget", None)
        if callable(forget):
            forget(message_id)

    def _show_home(self, update: Update):
        try:
            snapshot = self.client.health()
        except BridgeQueueError:
            self._error(update)
            return
        self._edit(update, self.home_text(snapshot, self.settings.enabled), self._home_markup(self.settings.enabled))

    def _show_page(self, update: Update, kind: str, page: int):
        try:
            items = self.client.active(100) if kind == "active" else self.client.dead(100)
        except BridgeQueueError:
            self._error(update)
            return
        text = self.active_text(items, page) if kind == "active" else self.dead_text(items, page)
        self._edit(update, text, self._page_markup(kind, items, page, self.settings.enabled))

    def command(self, update: Update, _context: CallbackContext):
        if self._allowed(update):
            self._show_home(update)

    def _blocked(self, update: Update):
        update.callback_query.answer("请先开启管理开关", show_alert=True)

    def _confirm_item(self, update: Update, action: str, message_id: str):
        if not self.settings.enabled:
            self._blocked(update)
            return
        update.callback_query.answer()
        labels = {
            "retry": "立即重试这条活动消息",
            "requeue": "重新投递这条死信",
            "discard": "放弃这条队列记录",
        }
        self._edit(
            update,
            f"确认：{labels.get(action, '执行此操作')}？\n\n放弃后会保留去重标记，但不会再保存消息正文。",
            self._confirm_markup(action, message_id),
        )

    def _execute_item(self, update: Update, action: str, message_id: str):
        if not self.settings.enabled:
            self._blocked(update)
            return
        try:
            if action == "retry":
                result = self.client.retry_active(message_id)
                text = {
                    "retried": "已重新加入投递队列。",
                    "inflight": "消息正在处理中，未直接改动。",
                    "not_found": "活动队列中已找不到这条消息。",
                }.get(result, "活动消息状态未改变。")
            elif action == "requeue":
                text = "已重新加入投递队列。" if self.client.requeue_dead(message_id) else "该消息已不在死信队列。"
                if text.startswith("已"):
                    self._forget_dead(message_id)
            else:
                result = self.client.discard(message_id, "telegram-admin")
                text = {
                    "discarded": "已放弃投递，并保留去重标记。",
                    "inflight": "消息正在处理中，未直接改动。",
                    "not_found": "该消息已不在可操作队列中。",
                }.get(result, "队列记录状态未改变。")
                if result == "discarded":
                    self._forget_dead(message_id)
        except BridgeQueueError:
            text = "操作失败，Bridge 暂时不可用。"
        update.callback_query.answer()
        self._edit(update, "Bridge 队列操作结果\n\n" + text, self._result_markup())

    def _confirm_batch(self, update: Update, action: str):
        if not self.settings.enabled:
            self._blocked(update)
            return
        update.callback_query.answer()
        label = "重新投递全部死信" if action == "requeue-all" else "放弃全部死信"
        self._edit(
            update,
            f"确认：{label}？\n\n这会处理当前全部死信记录。",
            self._confirm_markup(action),
        )

    def _execute_batch(self, update: Update, action: str):
        if not self.settings.enabled:
            self._blocked(update)
            return
        try:
            dead_ids = [str(item.get("id")) for item in self.client.dead(100) if item.get("id")]
            if action == "requeue-all":
                count = self.client.requeue_all_dead()
                text = f"已重新投递 {count} 条死信。"
            else:
                count = self.client.discard_all_dead("telegram-admin")
                text = f"已放弃 {count} 条死信，并保留去重标记。"
            if count:
                for message_id in dead_ids:
                    self._forget_dead(message_id)
        except BridgeQueueError:
            text = "操作失败，Bridge 暂时不可用。"
        update.callback_query.answer()
        self._edit(update, "Bridge 队列操作结果\n\n" + text, self._result_markup())

    def callback(self, update: Update, _context: CallbackContext):
        query = update.callback_query
        if not query or not self._allowed(update):
            if query:
                query.answer("无权执行", show_alert=True)
            return
        data = query.data or ""
        if data == "bridgeq:close":
            query.answer("面板已隐藏")
            query.message.delete()
            return
        if data == "bridgeq:noop":
            query.answer("处理中，暂不允许直接改动")
            return
        if data == "bridgeq:home":
            query.answer()
            self._show_home(update)
            return
        if data == "bridgeq:toggle":
            self.settings.enabled = not self.settings.enabled
            query.answer("管理开关已保存")
            self._show_home(update)
            return
        parts = data.split(":", 2)
        if len(parts) == 3 and parts[0] == "bridgeq" and parts[1] in ("active", "dead"):
            try:
                page = int(parts[2])
            except ValueError:
                query.answer("页码无效", show_alert=True)
                return
            query.answer()
            self._show_page(update, parts[1], page)
            return
        if len(parts) == 3 and parts[0] == "bridgeq":
            action, message_id = parts[1], parts[2]
            if action in ("retry", "requeue", "discard"):
                self._confirm_item(update, action, message_id)
                return
            if action in ("retry-confirm", "requeue-confirm", "discard-confirm"):
                self._execute_item(update, action.removesuffix("-confirm"), message_id)
                return
        if data in ("bridgeq:requeue-all", "bridgeq:discard-all"):
            self._confirm_batch(update, data.removeprefix("bridgeq:"))
            return
        if data in ("bridgeq:requeue-all-confirm", "bridgeq:discard-all-confirm"):
            self._execute_batch(update, data.removeprefix("bridgeq:").removesuffix("-confirm"))
            return
        query.answer("无效操作", show_alert=True)
