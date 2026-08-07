import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler

try:
    from .bridge_queue import BridgeQueueClient, BridgeQueueError, BridgeQueueSettings
except ImportError:  # pragma: no cover - keeps direct test loading compatible
    from efb_telegram_master.bridge_queue import BridgeQueueClient, BridgeQueueError, BridgeQueueSettings


LOGGER = logging.getLogger(__name__)
URL = re.compile(r"https?://[^\s]+")
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s]+")


def _safe_reason(value) -> str:
    text = URL.sub("[链接]", str(value or "未知错误"))
    text = PATH.sub("[路径]", text)
    return " ".join(text.split())[:120]


def alert_text(item: dict) -> str:
    attempts = int(item.get("attempts") or 0)
    reason = _safe_reason(item.get("last_error"))
    return (
        "EFB 附件进入死信队列\n\n"
        f"已尝试：{attempts} 次\n"
        f"原因：{reason}\n\n"
        "不会自动重启容器。可在确认微信附件仍可获取后，手动重新投递一次。"
    )


class BridgeDeadLetterGuard:
    def __init__(
        self,
        channel,
        state_path: Path = None,
        settings: BridgeQueueSettings = None,
        settings_path: Path = None,
        autostart: bool = True,
    ):
        self.channel = channel
        data_root = Path(os.getenv("EFB_DATA_ROOT", "/data"))
        self.state_path = Path(
            state_path
            or data_root / "operations" / "state" / "bridge-dead-alerts.json"
        )
        self.base_url = os.getenv(
            "COMWECHAT_BRIDGE_API_BASE", "http://comwechat:19088"
        ).rstrip("/")
        self.settings = settings or BridgeQueueSettings(
            settings_path
            or data_root / "operations" / "state" / "bridge-queue-settings.json"
        )
        self.queue_client = BridgeQueueClient(self.base_url)
        self.notified = self._load()
        channel.bot_manager.dispatcher.add_handler(
            CallbackQueryHandler(self.callback, pattern=r"^bridge:")
        )
        if autostart:
            threading.Thread(
                target=self.run,
                name="efb-bridge-dead-letter-guard",
                daemon=True,
            ).start()

    def _load(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return set(data.get("notified", [])) if isinstance(data, dict) else set()
        except (OSError, ValueError, TypeError):
            return set()

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.state_path.parent),
            prefix=".bridge-dead-alerts.",
            delete=False,
        ) as handle:
            json.dump({"notified": sorted(self.notified)}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, self.state_path)

    def _json(self, path: str, payload=None) -> dict:
        return self.queue_client._request(path, payload)

    def forget(self, message_id: str) -> None:
        if str(message_id) in self.notified:
            self.notified.discard(str(message_id))
            self._save()

    @staticmethod
    def keyboard(message_id: str):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "重新投递",
                callback_data=f"bridge:retry:{message_id}",
            ),
            InlineKeyboardButton("关闭页面", callback_data="bridge:close"),
        ]])

    @staticmethod
    def confirm_keyboard(message_id: str):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "确认重新投递",
                callback_data=f"bridge:retry-confirm:{message_id}",
            ),
            InlineKeyboardButton("取消", callback_data="bridge:close"),
        ]])

    def check_once(self):
        result = self._json("/v1/messages/dead?limit=20")
        for item in result.get("messages", []):
            message_id = str(item.get("id") or "")
            if not message_id or message_id in self.notified:
                continue
            sent = False
            for admin in self.channel.config["admins"]:
                self.channel.bot_manager.send_message(
                    admin,
                    alert_text(item),
                    reply_markup=self.keyboard(message_id),
                )
                sent = True
            if sent:
                self.notified.add(message_id)
                self._save()

    def run(self):
        time.sleep(30)
        while True:
            try:
                self.check_once()
            except Exception:
                LOGGER.exception("failed to check Bridge dead letters")
            time.sleep(60)

    def callback(self, update: Update, _context: CallbackContext):
        query = update.callback_query
        if not query:
            return
        chat = getattr(update, "effective_chat", None)
        if not (
            update.effective_user
            and update.effective_user.id in self.channel.config["admins"]
            and chat is not None
            and getattr(chat, "type", "") == "private"
        ):
            query.answer("无权执行", show_alert=True)
            return
        data = query.data or ""
        if data == "bridge:close":
            query.answer()
            query.message.delete()
            return
        if data.startswith("bridge:retry:"):
            if not self.settings.enabled:
                query.answer("请先在 Bridge 队列面板开启管理开关", show_alert=True)
                return
            message_id = data[len("bridge:retry:"):]
            if not message_id:
                query.answer("消息编号无效", show_alert=True)
                return
            query.answer()
            query.edit_message_reply_markup(reply_markup=self.confirm_keyboard(message_id))
            return
        if not data.startswith("bridge:retry-confirm:"):
            query.answer("无效操作", show_alert=True)
            return
        if not self.settings.enabled:
            query.answer("请先在 Bridge 队列面板开启管理开关", show_alert=True)
            return
        message_id = data[len("bridge:retry-confirm:"):]
        if not message_id:
            query.answer("消息编号无效", show_alert=True)
            return
        try:
            if not self.queue_client.requeue_dead(message_id):
                query.answer("该消息已不在死信队列", show_alert=True)
                return
            self.notified.discard(message_id)
            self._save()
            query.answer("已重新加入投递队列")
            query.edit_message_text("EFB 附件已重新加入投递队列。")
        except Exception:
            LOGGER.exception("failed to requeue Bridge dead letter")
            query.answer("重新投递失败，请稍后重试", show_alert=True)
