import json
import os
import re
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Dict, List
from urllib import request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext


SENSITIVE_KEY = re.compile(r"(?i)^(token|password|passwd|secret|api_hash|api_id|vncpass)$")
BOT_TOKEN = re.compile(r"bot\d+:[^/\s]+")
URL = re.compile(r"https?://[^\s]+")


def redact_error(value: str) -> str:
    value = BOT_TOKEN.sub("bot<redacted>", str(value))
    return URL.sub("<endpoint>", value)[:160]


def backup_summary(path: Path) -> dict:
    directories = sorted(item for item in path.iterdir() if item.is_dir()) if path.exists() else []
    total = 0
    for directory in directories:
        for item in directory.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    return {
        "count": len(directories),
        "latest": directories[-1].name if directories else "无",
        "bytes": total,
        "path": str(path),
    }


def scan_sensitive_keys(path: Path) -> List[dict]:
    findings = []
    if not path.exists():
        return findings
    for item in sorted(path.rglob("*")):
        if not item.is_file() or (item.suffix.lower() not in {".yaml", ".yml", ".json"}
                                  and item.name != ".env"):
            continue
        keys = set()
        try:
            for line in item.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                key = re.split(r"[:=]", stripped, maxsplit=1)[0].strip().strip('"\'')
                if SENSITIVE_KEY.match(key):
                    keys.add(key)
        except OSError:
            continue
        if keys:
            findings.append({"file": str(item.relative_to(path)), "keys": sorted(keys)})
    return findings


def _post_json(url: str, payload: bytes = b"{}", timeout: int = 3) -> dict:
    req = request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "未知"


class OperationsUI:
    def __init__(self, channel):
        self.channel = channel
        self.data_root = Path(os.getenv("EFB_DATA_ROOT", "/data"))

    @staticmethod
    def markup(refresh: str = "") -> InlineKeyboardMarkup:
        row = []
        if refresh:
            row.append(InlineKeyboardButton("刷新", callback_data=f"ops:{refresh}"))
        row.append(InlineKeyboardButton("关闭", callback_data="ops:close"))
        return InlineKeyboardMarkup([row])

    def _allowed(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.channel.config["admins"])

    def _send(self, update: Update, text: str, refresh: str = ""):
        markup = self.markup(refresh)
        if update.callback_query:
            update.callback_query.edit_message_text(text, reply_markup=markup)
        else:
            update.effective_message.reply_text(text, reply_markup=markup)

    def _wechat_login(self) -> str:
        try:
            result = _post_json("http://127.0.0.1:18888/api/?type=0")
            return "已登录" if result.get("is_login") == 1 else "已退出"
        except Exception as error:
            return f"检测失败（{redact_error(error)}）"

    def _bot_api(self) -> str:
        token = self.channel.config.get("token", "")
        endpoint = os.getenv("TELEGRAM_BOT_API", "http://127.0.0.1:8081").rstrip("/")
        try:
            result = _post_json(f"{endpoint}/bot{token}/getMe")
            return "正常" if result.get("ok") else "返回异常"
        except Exception as error:
            return f"不可用（{redact_error(error)}）"

    def health_text(self) -> str:
        backup = backup_summary(self.data_root / "backups")
        telemetry = self.data_root / "operations" / "state" / "delivery.json"
        last_delivery = "暂无记录"
        try:
            data = json.loads(telemetry.read_text(encoding="utf-8"))
            stamp = data.get("last_delivered_at") or data.get("last_inbound_at")
            if stamp:
                last_delivery = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError, TypeError):
            pass
        return (
            "EFB 运行状态\n\n"
            f"微信：{self._wechat_login()}\n"
            f"Telegram Bot API：{self._bot_api()}\n"
            f"最近消息活动：{last_delivery}\n"
            f"配置备份：{backup['count']} 份\n"
            f"持久化目录：{self.data_root}"
        )

    def health(self, update: Update, _context: CallbackContext):
        if self._allowed(update):
            self._send(update, self.health_text(), "health")

    def version(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        text = (
            "EFB 版本信息\n\n"
            f"EFB：{_package_version('ehforwarderbot')}\n"
            f"Telegram Master：{_package_version('efb-telegram-master')}\n"
            f"ComWeChat：{_package_version('efb-wechat-comwechat-slave')}\n"
            f"镜像版本：{os.getenv('EFB_IMAGE_REVISION', 'latest（源码已固定）')}"
        )
        self._send(update, text)

    def backup_info(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        result = backup_summary(self.data_root / "backups")
        text = (
            "EFB 配置备份\n\n"
            f"数量：{result['count']} 份\n"
            f"最近：{result['latest']}\n"
            f"占用：{_human_size(result['bytes'])}\n"
            f"路径：{result['path']}\n\n这里只显示状态，不传输配置内容。"
        )
        self._send(update, text, "backup")

    def filetest(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        local = self.channel.flag("local_bot_api")
        text = (
            "EFB 文件链路检测\n\n"
            f"本地 Bot API：{'已启用' if local else '未启用'}\n"
            f"接口状态：{self._bot_api()}\n"
            f"EFB 20MB 限制：{'已绕过' if local else '仍然生效'}\n"
            "说明：实际可上传大小仍受 Telegram 本地 Bot API 和磁盘空间限制。"
        )
        self._send(update, text, "filetest")

    def security(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        findings = scan_sensitive_keys(self.data_root / "profiles")
        if findings:
            lines = [f"- {item['file']}：{', '.join(item['keys'])}" for item in findings[:20]]
            detail = "\n".join(lines)
        else:
            detail = "未发现需要检查的敏感字段。"
        self._send(update, "EFB 配置安全检查\n\n" + detail + "\n\n只显示字段名，不显示字段值。", "security")

    def callback(self, update: Update, context: CallbackContext):
        query = update.callback_query
        if not query or not self._allowed(update):
            return
        action = (query.data or "").split(":", 1)[-1]
        if action == "close":
            query.answer()
            query.message.delete()
            return
        handlers = {
            "health": self.health,
            "backup": self.backup_info,
            "filetest": self.filetest,
            "security": self.security,
        }
        query.answer()
        handler = handlers.get(action)
        if handler:
            handler(update, context)
