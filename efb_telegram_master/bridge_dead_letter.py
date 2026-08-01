import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib import request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler


LOGGER = logging.getLogger(__name__)


def alert_text(item: dict) -> str:
    attempts = int(item.get("attempts") or 0)
    reason = str(item.get("last_error") or "未知错误")[:80]
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
        state_path: Path = Path("/data/operations/state/bridge-dead-alerts.json"),
        autostart: bool = True,
    ):
        self.channel = channel
        self.state_path = Path(state_path)
        self.base_url = os.getenv(
            "COMWECHAT_BRIDGE_API_BASE", "http://comwechat:19088"
        ).rstrip("/")
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
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result if isinstance(result, dict) else {}

    @staticmethod
    def keyboard(message_id: str):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "重新投递",
                callback_data=f"bridge:retry:{message_id}",
            ),
            InlineKeyboardButton("关闭页面", callback_data="bridge:close"),
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
        if not update.effective_user or update.effective_user.id not in self.channel.config["admins"]:
            query.answer("无权执行", show_alert=True)
            return
        data = query.data or ""
        if data == "bridge:close":
            query.answer()
            query.message.delete()
            return
        if not data.startswith("bridge:retry:"):
            query.answer("无效操作", show_alert=True)
            return
        message_id = data.removeprefix("bridge:retry:")
        try:
            result = self._json(
                "/v1/messages/requeue",
                {"message_id": message_id},
            )
            if result.get("requeued") != 1:
                query.answer("该消息已不在死信队列", show_alert=True)
                return
            self.notified.discard(message_id)
            self._save()
            query.answer("已重新加入投递队列")
            query.edit_message_text("EFB 附件已重新加入投递队列。")
        except Exception:
            LOGGER.exception("failed to requeue Bridge dead letter")
            query.answer("重新投递失败，请稍后重试", show_alert=True)
