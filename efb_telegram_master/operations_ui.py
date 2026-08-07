import json
import os
import re
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Dict, List
from urllib import request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from .bridge_queue_ui import BridgeQueueUI


SENSITIVE_KEY = re.compile(r"(?i)^(token|password|passwd|secret|api_hash|api_id|vncpass)$")
BOT_TOKEN = re.compile(r"bot\d+:[^/\s]+")
URL = re.compile(r"https?://[^\s]+")


def redact_error(value: str) -> str:
    value = BOT_TOKEN.sub("bot<redacted>", str(value))
    return URL.sub("<endpoint>", value)[:160]


def backup_summary(path: Path) -> dict:
    directories = [item for item in path.iterdir() if item.is_dir()] if path.exists() else []
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
        "latest": max(directories, key=lambda item: item.stat().st_mtime).name if directories else "无",
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
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "未知"


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _record_count(value) -> int:
    if isinstance(value, (dict, list)):
        return len(value)
    return 0


def delivery_summary(data_root: Path, reconcile: dict) -> dict:
    state_root = data_root / "operations" / "state"
    failed_records = load_json(state_root / "failed-deliveries.json")
    pending_records = load_json(
        data_root / "profiles" / "comwechat" / "honus.comwechat" / "pending-files.json"
    )
    try:
        pending = max(0, int(reconcile.get("pending_count", 0)))
    except (TypeError, ValueError):
        pending = 0
    try:
        failed = max(0, int(reconcile.get("failed_count", 0)))
    except (TypeError, ValueError):
        failed = 0
    pending = max(pending, _record_count(pending_records))
    failed = max(failed, _record_count(failed_records))
    persisted = sum(
        1
        for record in failed_records.values()
        if isinstance(record, dict)
        and (
            record.get("storage") == "durable"
            or str(record.get("path", "")).find("failed-media") >= 0
        )
    )
    return {
        "pending": pending,
        "failed": failed,
        "persisted_failed_media": persisted,
    }


def _duration_text(seconds) -> str:
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "未知"
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}分{remainder}秒"
    if minutes:
        return f"{minutes}分钟"
    return f"{remainder}秒"


def format_timestamp(value) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "暂无"


def format_uptime(started_at, now=None) -> str:
    try:
        elapsed = max(0, int((time.time() if now is None else now) - float(started_at)))
    except (TypeError, ValueError, OSError):
        return "暂无"
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    if minutes or not parts:
        parts.append(f"{minutes}分钟")
    return " ".join(parts)


class OperationsUI:
    def __init__(self, channel):
        self.channel = channel
        self.data_root = Path(os.getenv("EFB_DATA_ROOT", "/data"))
        self.started_at = time.time()
        self.bridge_queue_ui = BridgeQueueUI(
            channel,
            settings=getattr(channel, "bridge_queue_settings", None),
        )

    @staticmethod
    def markup(refresh: str = "", include_bridge: bool = False) -> InlineKeyboardMarkup:
        rows = []
        if include_bridge:
            rows.append([
                InlineKeyboardButton("投递明细", callback_data="ops:delivery"),
                InlineKeyboardButton("异常中心", callback_data="ops:errors"),
                InlineKeyboardButton("失败诊断", callback_data="ops:diagnostic"),
            ])
            row = [InlineKeyboardButton("Bridge 队列", callback_data="bridgeq:home")]
            if refresh:
                row.append(InlineKeyboardButton("刷新", callback_data=f"ops:{refresh}"))
            row.append(InlineKeyboardButton("关闭", callback_data="ops:close"))
            rows.append(row)
        else:
            row = []
            if refresh:
                row.append(InlineKeyboardButton("刷新", callback_data=f"ops:{refresh}"))
            row.append(InlineKeyboardButton("关闭", callback_data="ops:close"))
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    def _allowed(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.channel.config["admins"])

    def _send(self, update: Update, text: str, refresh: str = "", include_bridge: bool = False):
        markup = self.markup(refresh, include_bridge=include_bridge)
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

    def _watchdog_state(self) -> dict:
        control = getattr(self.channel, "watchdog_control", None)
        if control is None:
            return {}
        try:
            state = control.get_state()
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def _bridge_queue_summary(self) -> str:
        bridge_ui = getattr(self, "bridge_queue_ui", None)
        client = getattr(bridge_ui, "client", None)
        if client is None:
            return "检测失败"
        try:
            snapshot = client.health()
            staged = int(snapshot.get("staged_size", 0) or 0)
            pending = int(snapshot.get("pending_size", 0) or 0)
            inflight = int(snapshot.get("inflight_size", 0) or 0)
            total = int(snapshot.get("queue_size", staged + pending + inflight) or 0)
            dead = int(snapshot.get("dead_letter_size", 0) or 0)
            return f"暂存 {staged}｜待投递 {pending}｜处理中 {inflight}｜总计 {total}｜死信 {dead}"
        except Exception as error:
            return f"不可用（{redact_error(error)}）"

    def health_text(self) -> str:
        backup = backup_summary(self.data_root / "backups")
        state_root = self.data_root / "operations" / "state"
        delivery = load_json(state_root / "delivery.json")
        health = load_json(state_root / "health-guard.json")
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        database = load_json(self.data_root / "database-audit-latest.json")
        capacity = load_json(self.data_root / "capacity-audit-latest.json")
        upstream = load_json(self.data_root / "upstream-audit-latest.json")
        watchdog = self._watchdog_state()
        spoiler_store = getattr(self.channel, "author_name_spoiler_store", None)
        spoiler_enabled = bool(getattr(spoiler_store, "enabled", False))
        last_delivery = format_timestamp(
            delivery.get("last_delivered_at") or delivery.get("last_inbound_at")
        )
        stack_status = "正常" if health.get("healthy") else health.get("reason", "等待首次检查")
        database_status = "正常" if database.get("healthy") else "等待检查或异常"
        disk = capacity.get("disk") or {}
        free_percent = disk.get("free_percent")
        disk_text = f"{float(free_percent):.2f}%" if isinstance(free_percent, (int, float)) else "等待检查"
        queue = delivery_summary(self.data_root, reconcile)
        bridge_summary = self._bridge_queue_summary()
        updates = upstream.get("update_count", 0)
        if watchdog:
            recovery_text = (
                "总开关" + ("开启" if watchdog.get("master_enabled") else "关闭")
                + "｜全天" + ("开启" if watchdog.get("event_enabled") else "关闭")
                + "｜凌晨" + ("开启" if watchdog.get("night_enabled") else "关闭")
            )
            recovery_window = (
                f"{watchdog.get('daily_start', '02:50')}-"
                f"{watchdog.get('daily_end', '03:50')}"
            )
            recovery_config = (
                f"每{_duration_text(watchdog.get('poll_seconds', 120))}检查｜"
                f"冷却{_duration_text(watchdog.get('click_cooldown_seconds', 120))}｜"
                f"连续失败{watchdog.get('max_recovery_failures', 3)}次暂停"
            )
            diagnostic_retention = watchdog.get("diagnostic_retention", "仅保留最新一张")
        else:
            recovery_text = "等待检查"
            recovery_window = "等待检查"
            recovery_config = "等待检查"
            diagnostic_retention = "等待检查"
        return (
            "EFB 综合状态\n\n"
            f"EFB 运行时间：{format_uptime(self.started_at)}\n"
            f"微信：{self._wechat_login()}\n"
            f"Telegram Bot API：{self._bot_api()}\n"
            f"四容器与共享网络：{stack_status}\n"
            f"最近恢复动作：{health.get('action', '暂无')}\n"
            f"自动恢复：{recovery_text}\n"
            f"恢复时段：{recovery_window}\n"
            f"恢复配置：{recovery_config}\n"
            f"失败诊断：{diagnostic_retention}\n"
            f"群成员姓名隐藏：{'开启' if spoiler_enabled else '关闭'}\n"
            f"最近消息活动：{last_delivery}\n"
            f"投递队列：待处理 {queue['pending']}｜失败 {queue['failed']}\n"
            f"Bridge 队列：{bridge_summary}\n"
            f"失败附件已持久化：{queue['persisted_failed_media']} 条\n"
            f"映射数据库：{database_status}\n"
            f"NAS 磁盘剩余：{disk_text}\n"
            f"待评估上游更新：{updates} 项\n"
            f"配置备份：{backup['count']} 份｜{_human_size(backup['bytes'])}\n"
            f"镜像版本：{os.getenv('EFB_IMAGE_REVISION', '未知')}"
        )

    def health(self, update: Update, _context: CallbackContext):
        if self._allowed(update):
            self._send(update, self.health_text(), "status", include_bridge=True)

    def delivery_detail(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        state_root = self.data_root / "operations" / "state"
        delivery = load_json(state_root / "delivery.json")
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        queue = delivery_summary(self.data_root, reconcile)
        self._send(
            update,
            "EFB 投递明细\n\n"
            f"待处理：{queue['pending']} 条\n"
            f"失败：{queue['failed']} 条\n"
            f"失败附件已持久化：{queue['persisted_failed_media']} 条\n"
            f"最近入站：{format_timestamp(delivery.get('last_inbound_at'))}\n"
            f"最近投递：{format_timestamp(delivery.get('last_delivered_at'))}\n"
            f"最近失败：{format_timestamp(delivery.get('last_failed_at'))}",
            "delivery",
        )

    def errors(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        state_root = self.data_root / "operations" / "state"
        health = load_json(state_root / "health-guard.json")
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        queue = delivery_summary(self.data_root, reconcile)
        watchdog = self._watchdog_state()
        self._send(
            update,
            "EFB 异常中心\n\n"
            f"微信：{self._wechat_login()}\n"
            f"服务健康：{'正常' if health.get('healthy') else health.get('reason', '未检查')}\n"
            f"最近守护动作：{health.get('action', '暂无')}\n"
            f"自动恢复：{('总开关开启' if watchdog.get('master_enabled') else '总开关关闭') if watchdog else '等待检查'}\n"
            f"待处理投递：{queue['pending']} 条\n"
            f"失败投递：{queue['failed']} 条\n"
            "详情请查看日志或 Bridge 队列。",
            "errors",
        )

    def diagnostic(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        path = self.data_root / "watchdog" / "diagnostics" / "last-login-failure.png"
        if not path.is_file():
            self._send(update, "EFB 失败诊断\n\n当前没有保存失败诊断画面。", "diagnostic")
            return
        caption = f"EFB 最新失败诊断画面\n时间：{format_timestamp(path.stat().st_mtime)}"
        with path.open("rb") as photo:
            update.effective_message.reply_photo(photo=photo, caption=caption)

    def status(self, update: Update, context: CallbackContext):
        self.health(update, context)

    def bridge(self, update: Update, context: CallbackContext):
        self.bridge_queue_ui.command(update, context)

    def bridge_callback(self, update: Update, context: CallbackContext):
        self.bridge_queue_ui.callback(update, context)

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
            "status": self.status,
            "delivery": self.delivery_detail,
            "errors": self.errors,
            "diagnostic": self.diagnostic,
            "backup": self.backup_info,
            "filetest": self.filetest,
            "security": self.security,
        }
        query.answer()
        handler = handlers.get(action)
        if handler:
            handler(update, context)
