import json
import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Dict, List
from urllib import request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext
from ehforwarderbot import coordinator

from .bridge_queue_ui import BridgeQueueUI
from .delivery_telemetry import delivery_stats_summary
from .failed_media import cleanup_failed_media


SENSITIVE_KEY = re.compile(r"(?i)^(token|password|passwd|secret|api_hash|api_id|vncpass)$")
BOT_TOKEN = re.compile(r"bot\d+:[^/\s]+")
URL = re.compile(r"https?://[^\s]+")
DELIVERY_PAGE_SIZE = 5
RECONCILE_MAX_AGE_SECONDS = 3 * 60 * 60
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


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


def _finder_feed_summary(channel) -> str:
    status = getattr(channel, "finder_feed_status", None)
    if not callable(status):
        return "未启用"
    try:
        result = status()
        return (
            f"等待 {result.get('waiting', 0)}｜请求 {result.get('requested', 0)}｜"
            f"处理中 {result.get('processing', 0)}｜失败 {result.get('failed', 0)}"
        )
    except Exception:
        return "检查失败"


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def image_metadata(data_root: Path) -> dict:
    metadata = load_json(data_root / "operations" / "state" / "image-metadata.json")
    if not metadata.get("build_time"):
        metadata["build_time"] = os.getenv("EFB_IMAGE_BUILD_TIME")
    if not metadata.get("revision"):
        metadata["revision"] = os.getenv("EFB_IMAGE_SOURCE_REF")
    return metadata


def _record_count(value) -> int:
    if isinstance(value, (dict, list)):
        return len(value)
    return 0


def _reconcile_is_fresh(reconcile: dict, now=None) -> bool:
    checked_at = reconcile.get("checked_at")
    if checked_at is None:
        return True
    try:
        age = (time.time() if now is None else now) - float(checked_at)
        max_age = int(os.getenv(
            "EFB_RECONCILE_MAX_AGE_SECONDS",
            RECONCILE_MAX_AGE_SECONDS,
        ))
    except (TypeError, ValueError):
        return False
    return 0 <= age <= max(0, max_age)


def _record_is_active(record: dict, now=None) -> bool:
    expires = record.get("expires")
    if expires is None:
        return True
    try:
        return float(expires) > (time.time() if now is None else now)
    except (TypeError, ValueError):
        return True


def _active_record_count(records: dict, now=None) -> int:
    now = time.time() if now is None else now
    count = 0
    for record in records.values():
        if not isinstance(record, dict):
            continue
        if _record_is_active(record, now):
            count += 1
    return count


def delivery_summary(data_root: Path, reconcile: dict) -> dict:
    state_root = data_root / "operations" / "state"
    failed_path = state_root / "failed-deliveries.json"
    pending_path = (
        data_root / "profiles" / "comwechat" / "honus.comwechat" / "pending-files.json"
    )
    failed_records = load_json(failed_path)
    pending_records = load_json(pending_path)
    reconcile_fresh = _reconcile_is_fresh(reconcile)
    try:
        report_pending = max(0, int(reconcile.get("pending_count", 0)))
    except (TypeError, ValueError):
        report_pending = 0
    try:
        report_failed = max(0, int(reconcile.get("failed_count", 0)))
    except (TypeError, ValueError):
        report_failed = 0
    pending = _record_count(pending_records) if pending_path.is_file() else (
        report_pending if reconcile_fresh else 0
    )
    failed = _active_record_count(failed_records) if failed_path.is_file() else (
        report_failed if reconcile_fresh else 0
    )
    now = time.time()
    persisted = sum(
        1
        for record in failed_records.values()
        if isinstance(record, dict)
        and (
            record.get("expires") is None
            or _record_is_active(record, now)
        )
        and (
            record.get("storage") == "durable"
            or str(record.get("path", "")).find("failed-media") >= 0
        )
    )
    return {
        "pending": pending,
        "failed": failed,
        "persisted_failed_media": persisted,
        "reconcile_stale": not reconcile_fresh,
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
        timestamp = float(value)
        if timestamp <= 0:
            return "暂无"
        return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "暂无"


def format_audit_status(report: dict) -> str:
    if not report:
        return "未检查"
    status = "正常" if report.get("healthy", True) else "异常"
    return f"{status}（检查时间{format_timestamp(report.get('checked_at'))}）"


def format_delivery_stats(stats: dict) -> str:
    if not isinstance(stats, dict):
        stats = {}
    try:
        average = stats.get("average_latency_ms")
        average_text = "暂无" if average is None else (
            f"{float(average):.0f} 毫秒"
            if float(average) < 1000
            else f"{float(average) / 1000:.2f} 秒"
        )
    except (TypeError, ValueError):
        average_text = "暂无"
    return (
        f"微信接收 {int(stats.get('inbound', 0) or 0)}｜"
        f"Telegram成功 {int(stats.get('delivered', 0) or 0)}｜"
        f"过滤 {int(stats.get('filtered', 0) or 0)}｜"
        f"失败 {int(stats.get('failed', 0) or 0)}｜"
        f"平均延迟 {average_text}"
    )


def format_backup_verification(report: dict) -> str:
    if not report:
        return "未检查"
    status = "正常" if report.get("healthy", False) else "异常"
    checks = []
    manifest = report.get("manifest") or {}
    sqlite = report.get("sqlite") or {}
    decrypt = report.get("decrypt") or {}
    if manifest.get("status") == "ok":
        checks.append("清单")
    if sqlite.get("status") == "ok":
        checks.append("SQLite")
    if decrypt.get("status") == "ok":
        checks.append("解密")
    elif decrypt.get("status") == "not_configured":
        checks.append("解密未配置")
    detail = f"（{'、'.join(checks)}）" if checks else ""
    return f"{status}{detail}（检查时间{format_timestamp(report.get('checked_at'))}）"


def format_maintenance_status(state: dict) -> str:
    if not isinstance(state, dict) or not state:
        return "关闭"
    if state.get("enabled") or state.get("phase") not in (None, "", "idle"):
        return f"进行中（{_clean_text(state.get('phase'), 30)}）"
    result = state.get("last_result")
    if result == "rollback":
        return "关闭（上次已回滚）"
    if result == "failed":
        return "关闭（上次失败）"
    return "关闭"


def format_manual_restart(state: dict) -> str:
    if not isinstance(state, dict) or not state:
        return "暂无"
    status = state.get("status")
    if status == "requested":
        return "等待执行"
    if status == "running":
        return "执行中"
    if status == "completed":
        return f"最近完成 {format_timestamp(state.get('completed_at'))}"
    if status == "failed":
        return f"失败（{_clean_text(state.get('reason'), 50)}）"
    return "暂无"


def request_manual_restart(path: Path, now=None, requested_by=None) -> dict:
    path = Path(path)
    existing = load_json(path)
    if existing.get("status") in {"requested", "running"}:
        return existing
    now = time.time() if now is None else float(now)
    payload = {
        "version": 1,
        "request_id": f"manual-{int(now * 1000)}",
        "scope": "all",
        "status": "requested",
        "requested_at": now,
    }
    if requested_by is not None:
        payload["requested_by"] = int(requested_by)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.tmp").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.with_name(f".{path.name}.tmp").replace(path)
    return payload


def format_session_timestamp(value) -> str:
    try:
        timestamp = float(value)
        if timestamp <= 0:
            return "暂无"
        return datetime.fromtimestamp(
            timestamp,
            SHANGHAI_TIMEZONE,
        ).strftime("%Y-%m-%d %H:%M:%S")
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


def format_image_build_time(value) -> str:
    if value in (None, ""):
        return "未知"
    try:
        return format_session_timestamp(float(value))
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    try:
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, TypeError, ValueError):
        return _clean_text(text, 40)


def format_queue_latency(delivery: dict, now=None) -> str:
    pending = delivery.get("pending") if isinstance(delivery, dict) else None
    if isinstance(pending, dict) and pending.get("at") is not None:
        try:
            elapsed_ms = max(
                0.0,
                ((time.time() if now is None else now) - float(pending["at"])) * 1000,
            )
            if elapsed_ms < 1000:
                return f"当前等待 {elapsed_ms:.0f} 毫秒"
            return f"当前等待 {elapsed_ms / 1000:.2f} 秒"
        except (TypeError, ValueError):
            pass
    try:
        latency_ms = max(0.0, float(delivery.get("last_latency_ms")))
    except (AttributeError, TypeError, ValueError):
        return "暂无"
    if latency_ms < 1000:
        return f"最近完成 {latency_ms:.0f} 毫秒"
    return f"最近完成 {latency_ms / 1000:.2f} 秒"


def format_latest_match(metadata: dict) -> str:
    value = metadata.get("latest_match") if isinstance(metadata, dict) else None
    if isinstance(value, bool):
        status = "匹配" if value else "不匹配"
    elif str(value).lower() in {"true", "yes", "1", "match", "matched"}:
        status = "匹配"
    elif str(value).lower() in {"false", "no", "0", "mismatch", "unmatched"}:
        status = "不匹配"
    else:
        status = "未校验"
    checked = format_timestamp(metadata.get("checked_at")) if isinstance(metadata, dict) else "暂无"
    return f"{status}（最近校验{checked}）"


def runtime_version_text() -> str:
    return (
        f"EFB {_package_version('ehforwarderbot')}｜"
        f"Telegram Master {_package_version('efb-telegram-master')}｜"
        f"ComWechat {_package_version('efb-wechat-comwechat-slave')}"
    )


def _clean_text(value, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] or "暂无"


def _record_path(record: dict) -> str:
    message = record.get("msg") if isinstance(record.get("msg"), dict) else {}
    return str(record.get("path") or message.get("filepath") or "")


def _record_time(record: dict) -> float:
    message = record.get("msg") if isinstance(record.get("msg"), dict) else {}
    for value in (record.get("created_at"), message.get("timestamp")):
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


class OperationsUI:
    def __init__(self, channel):
        self.channel = channel
        self.data_root = Path(os.getenv("EFB_DATA_ROOT", "/data"))
        self.started_at = time.time()
        self.bridge_queue_ui = BridgeQueueUI(
            channel,
            settings=getattr(channel, "bridge_queue_settings", None),
        )
        self._status_source_messages = {}

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
            row.append(InlineKeyboardButton("全部重启", callback_data="ops:restart-all"))
            if refresh:
                row.append(InlineKeyboardButton("刷新", callback_data=f"ops:{refresh}"))
            rows.append(row)
            rows.append([InlineKeyboardButton("关闭并删除", callback_data="ops:status-close")])
        else:
            row = []
            if refresh:
                row.append(InlineKeyboardButton("刷新", callback_data=f"ops:{refresh}"))
            row.append(InlineKeyboardButton("关闭", callback_data="ops:close"))
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    def _allowed(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.channel.config["admins"])

    def _send(
        self,
        update: Update,
        text: str,
        refresh: str = "",
        include_bridge: bool = False,
        track_status_source: bool = False,
    ):
        markup = self.markup(refresh, include_bridge=include_bridge)
        if update.callback_query:
            result = update.callback_query.edit_message_text(text, reply_markup=markup)
        else:
            result = update.effective_message.reply_text(text, reply_markup=markup)
        if track_status_source and not update.callback_query and result:
            source = update.effective_message
            self._status_source_messages[(result.chat.id, result.message_id)] = (
                source.chat.id,
                source.message_id,
            )

    @staticmethod
    def _delivery_key(kind: str, key: str) -> str:
        if kind == "pending":
            return hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:12]
        return str(key)

    @staticmethod
    def delivery_markup(pending: int, failed: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"待处理 {pending} 条",
                    callback_data="ops:delivery:list:pending:0",
                ),
                InlineKeyboardButton(
                    f"失败 {failed} 条",
                    callback_data="ops:delivery:list:failed:0",
                ),
            ],
            [
                InlineKeyboardButton("刷新", callback_data="ops:delivery"),
                InlineKeyboardButton("关闭", callback_data="ops:close"),
            ],
        ])

    @staticmethod
    def delivery_list_markup(
        kind: str,
        records: List[tuple],
        page: int = 0,
    ) -> InlineKeyboardMarkup:
        total_pages = max(1, (len(records) + DELIVERY_PAGE_SIZE - 1) // DELIVERY_PAGE_SIZE)
        page = min(max(0, int(page)), total_pages - 1)
        start = page * DELIVERY_PAGE_SIZE
        page_records = records[start:start + DELIVERY_PAGE_SIZE]
        rows = []
        for index, (key, _record) in enumerate(page_records, start=start + 1):
            identity = OperationsUI._delivery_key(kind, key)
            buttons = [InlineKeyboardButton(
                f"查看 {index}",
                callback_data=f"ops:delivery:view:{kind}:{identity}",
            )]
            if kind == "pending":
                buttons.extend([
                    InlineKeyboardButton(
                        "立即投递",
                        callback_data=f"ops:delivery:push:{identity}",
                    ),
                    InlineKeyboardButton(
                        "删除",
                        callback_data=f"ops:delivery:delete:pending:{identity}",
                    ),
                ])
            else:
                buttons.extend([
                    InlineKeyboardButton(
                        "重新投递",
                        callback_data=f"ops:delivery:retry:{identity}",
                    ),
                    InlineKeyboardButton(
                        "删除",
                        callback_data=f"ops:delivery:delete:failed:{identity}",
                    ),
                ])
            rows.append(buttons)
        if not page_records:
            rows.append([InlineKeyboardButton("当前没有可管理记录", callback_data="ops:delivery")])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "上一页", callback_data=f"ops:delivery:list:{kind}:{page - 1}"
            ))
        navigation.append(InlineKeyboardButton("返回投递明细", callback_data="ops:delivery"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton(
                "下一页", callback_data=f"ops:delivery:list:{kind}:{page + 1}"
            ))
        rows.append(navigation)
        rows.append([InlineKeyboardButton("关闭", callback_data="ops:close")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _delivery_result_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("返回投递明细", callback_data="ops:delivery")],
            [InlineKeyboardButton("关闭", callback_data="ops:close")],
        ])

    @staticmethod
    def _delivery_confirm_markup(action: str, kind: str, identity: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "确认",
                    callback_data=f"ops:delivery:{action}-confirm:{kind}:{identity}",
                ),
                InlineKeyboardButton(
                    "取消",
                    callback_data=f"ops:delivery:list:{kind}:0",
                ),
            ],
        ])

    def _render(self, update: Update, text: str, markup: InlineKeyboardMarkup):
        if update.callback_query:
            update.callback_query.edit_message_text(text, reply_markup=markup)
        elif update.effective_message:
            update.effective_message.reply_text(text, reply_markup=markup)

    def _pending_records(self) -> List[tuple]:
        data = load_json(
            self.data_root / "profiles" / "comwechat" / "honus.comwechat" / "pending-files.json"
        )
        records = [
            (str(key), value)
            for key, value in data.items()
            if isinstance(value, dict)
        ]
        return sorted(records, key=lambda item: _record_time(item[1]), reverse=True)

    def _failed_store(self):
        slave_messages = getattr(self.channel, "slave_messages", None)
        return getattr(slave_messages, "failure_store", None)

    def _failed_records(self) -> List[tuple]:
        store = self._failed_store()
        if store is not None and callable(getattr(store, "items", None)):
            records = store.items()
        else:
            records = [
                (str(key), value)
                for key, value in load_json(
                    self.data_root / "operations" / "state" / "failed-deliveries.json"
                ).items()
                if isinstance(value, dict)
            ]
        return sorted(records, key=lambda item: _record_time(item[1]), reverse=True)

    def _find_delivery_record(self, kind: str, identity: str):
        records = self._pending_records() if kind == "pending" else self._failed_records()
        for key, record in records:
            if self._delivery_key(kind, key) == identity:
                return key, record
        return None

    def _delivery_list_text(self, kind: str, records: List[tuple], page: int) -> str:
        total_pages = max(1, (len(records) + DELIVERY_PAGE_SIZE - 1) // DELIVERY_PAGE_SIZE)
        page = min(max(0, int(page)), total_pages - 1)
        start = page * DELIVERY_PAGE_SIZE
        page_records = records[start:start + DELIVERY_PAGE_SIZE]
        title = "待处理投递" if kind == "pending" else "失败投递"
        lines = [f"EFB {title}（第 {page + 1}/{total_pages} 页）", ""]
        if not page_records:
            lines.append("当前没有可管理记录。")
        for index, (key, record) in enumerate(page_records, start=start + 1):
            message = record.get("msg") if isinstance(record.get("msg"), dict) else {}
            if kind == "pending":
                filename = Path(_record_path(record)).name or "附件路径未记录"
                lines.append(
                    f"{index}. {_clean_text(record.get('chat_name'), 30)}｜"
                    f"{_clean_text(record.get('author_name'), 24)}｜{_clean_text(filename, 40)}"
                )
            else:
                filename = record.get("filename") or Path(_record_path(record)).name
                lines.append(
                    f"{index}. {_clean_text(record.get('chat'), 30)}｜"
                    f"{_clean_text(filename, 40)}｜{format_timestamp(record.get('created_at'))}"
                )
                if record.get("error"):
                    lines.append(f"   原因：{redact_error(_clean_text(record.get('error'), 100))}")
        if kind == "pending":
            lines.extend([
                "",
                "待处理文件由 ComWeChat 自动投递；“立即投递”会跳过稳定等待，",
                "“删除”只删除待发记录，不删除微信原文件。",
            ])
        else:
            lines.extend(["", "失败记录支持查看、重新投递或删除；操作均需确认。"])
        return "\n".join(lines)

    def _delivery_record_text(self, kind: str, key: str, record: dict) -> str:
        message = record.get("msg") if isinstance(record.get("msg"), dict) else {}
        if kind == "pending":
            filename = Path(_record_path(record)).name or "附件路径未记录"
            return (
                "EFB 待处理投递\n\n"
                f"文件：{_clean_text(filename, 80)}\n"
                f"会话：{_clean_text(record.get('chat_name'), 80)}\n"
                f"联系人：{_clean_text(record.get('author_name'), 80)}\n"
                f"类型：{_clean_text(message.get('type'), 30)}\n"
                f"进入时间：{format_timestamp(_record_time(record))}\n\n"
                "状态：ComWeChat 正在等待附件准备或稳定。\n"
                "立即投递会跳过稳定等待；删除只移除待发记录，不删除原文件。"
            )
        filename = record.get("filename") or Path(_record_path(record)).name or "附件未记录"
        return (
            "EFB 失败投递\n\n"
            f"文件：{_clean_text(filename, 80)}\n"
            f"会话：{_clean_text(record.get('chat'), 80)}\n"
            f"类型：{_clean_text(record.get('type'), 30)}\n"
            f"失败时间：{format_timestamp(record.get('created_at'))}\n"
            f"过期时间：{format_timestamp(record.get('expires'))}\n"
            f"原因：{redact_error(_clean_text(record.get('error'), 140))}\n"
            f"附件：{'已持久化' if record.get('storage') == 'durable' else '未持久化'}"
        )

    def _comwechat_channel(self):
        try:
            return coordinator.get_module_by_id("honus.comwechat")
        except (KeyError, AttributeError, TypeError):
            return None

    def _show_delivery_list(self, update: Update, kind: str, page: int):
        records = self._pending_records() if kind == "pending" else self._failed_records()
        self._render(
            update,
            self._delivery_list_text(kind, records, page),
            self.delivery_list_markup(kind, records, page),
        )

    def _show_delivery_record(self, update: Update, kind: str, identity: str):
        found = self._find_delivery_record(kind, identity)
        if not found:
            self._render(update, "这条投递记录已不存在或已过期。", self._delivery_result_markup())
            return
        key, record = found
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "立即投递" if kind == "pending" else "重新投递",
                    callback_data=(
                        f"ops:delivery:push:{identity}"
                        if kind == "pending"
                        else f"ops:delivery:retry:{identity}"
                    ),
                ),
                InlineKeyboardButton(
                    "删除",
                    callback_data=f"ops:delivery:delete:{kind}:{identity}",
                ),
            ],
            [InlineKeyboardButton(
                "返回列表", callback_data=f"ops:delivery:list:{kind}:0"
            )],
            [InlineKeyboardButton("关闭", callback_data="ops:close")],
        ])
        self._render(update, self._delivery_record_text(kind, key, record), markup)

    def _confirm_delivery_action(self, update: Update, action: str, kind: str, identity: str):
        labels = {
            "push": "立即投递这条待处理附件",
            "retry": "重新投递这条失败附件",
            "delete": "删除这条投递记录",
        }
        note = ""
        if action == "delete" and kind == "pending":
            note = "\n\n只移除待发记录，不删除微信原文件。"
        elif action == "delete":
            note = "\n\n只删除失败记录及 EFB 保存的失败副本，不删除微信原文件。"
        update.callback_query.answer()
        self._render(
            update,
            f"确认：{labels[action]}？{note}",
            self._delivery_confirm_markup(action, kind, identity),
        )

    def _execute_delivery_action(self, update: Update, action: str, kind: str, identity: str):
        found = self._find_delivery_record(kind, identity)
        if not found:
            text = "这条投递记录已不存在或已过期。"
        elif action in ("push", "delete") and kind == "pending":
            key, _record = found
            slave = self._comwechat_channel()
            method_name = "request_pending_file_delivery" if action == "push" else "remove_pending_file"
            method = getattr(slave, method_name, None) if slave is not None else None
            if not callable(method):
                text = "ComWeChat 当前版本不支持此项队列操作。"
            else:
                result = method(key)
                text = {
                    "queued": "已请求立即投递，ComWeChat 将继续处理。",
                    "removed": "已删除待处理记录，微信原文件未删除。",
                    "not_found": "待处理记录已不存在。",
                    "not_ready": "原文件尚未准备好，暂未强制投递。",
                }.get(result, "待处理记录状态未改变。")
        elif action == "retry" and kind == "failed":
            token, record = found
            slave_messages = getattr(self.channel, "slave_messages", None)
            retry = getattr(slave_messages, "_retry_persisted", None)
            path = record.get("path")
            if not callable(retry):
                text = "失败投递处理器当前不可用。"
            elif not path or not os.path.isfile(path):
                text = "失败附件副本已不存在，无法重新投递。"
            else:
                try:
                    retry(token, record)
                except Exception as error:
                    logger = getattr(self.channel, "logger", None)
                    if logger is not None:
                        logger.exception("运维面板重新投递失败: token=%s", token)
                    text = f"重新投递失败：{redact_error(error)}"
                else:
                    text = "已重新投递；成功后该失败记录和失败副本会自动清理。"
        elif action == "delete" and kind == "failed":
            token, record = found
            store = self._failed_store()
            if store is None:
                text = "失败记录存储当前不可用。"
            else:
                store.remove(token)
                slave_messages = getattr(self.channel, "slave_messages", None)
                root = getattr(slave_messages, "failed_media_root", self.data_root / "operations" / "failed-media")
                cleaned = cleanup_failed_media(record.get("path", ""), root)
                text = (
                    "已删除失败记录；EFB 保存的失败副本已清理。"
                    if cleaned
                    else "已删除失败记录；未找到对应的 EFB 失败副本。"
                )
        else:
            text = "投递操作与记录类型不匹配。"
        update.callback_query.answer()
        self._render(update, "EFB 投递操作结果\n\n" + text, self._delivery_result_markup())

    def delivery_callback(self, update: Update, _context: CallbackContext):
        query = update.callback_query
        if not query or not self._allowed(update):
            if query:
                query.answer("无权执行", show_alert=True)
            return
        parts = (query.data or "").split(":")
        if len(parts) < 3 or parts[:2] != ["ops", "delivery"]:
            query.answer("无效操作", show_alert=True)
            return
        if parts[2] == "list" and len(parts) == 5 and parts[3] in ("pending", "failed"):
            try:
                page = int(parts[4])
            except ValueError:
                query.answer("页码无效", show_alert=True)
                return
            query.answer()
            self._show_delivery_list(update, parts[3], page)
            return
        if parts[2] == "view" and len(parts) == 5 and parts[3] in ("pending", "failed"):
            query.answer()
            self._show_delivery_record(update, parts[3], parts[4])
            return
        if parts[2] in ("push", "retry") and len(parts) == 4:
            self._confirm_delivery_action(
                update,
                parts[2],
                "pending" if parts[2] == "push" else "failed",
                parts[3],
            )
            return
        if parts[2] in ("push-confirm", "retry-confirm") and len(parts) == 5:
            self._execute_delivery_action(update, parts[2][:-8], parts[3], parts[4])
            return
        if parts[2] == "delete" and len(parts) == 5 and parts[3] in ("pending", "failed"):
            self._confirm_delivery_action(update, "delete", parts[3], parts[4])
            return
        if parts[2] == "delete-confirm" and len(parts) == 5 and parts[3] in ("pending", "failed"):
            self._execute_delivery_action(update, "delete", parts[3], parts[4])
            return
        query.answer("无效操作", show_alert=True)

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
        image = image_metadata(self.data_root)
        health = load_json(state_root / "health-guard.json")
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        database = load_json(self.data_root / "database-audit-latest.json")
        capacity = load_json(self.data_root / "capacity-audit-latest.json")
        upstream = load_json(self.data_root / "upstream-audit-latest.json")
        backup_audit = load_json(self.data_root / "backup-audit-latest.json")
        maintenance = load_json(state_root / "maintenance.json")
        manual_restart = load_json(state_root / "manual-restart.json")
        session_events = load_json(
            self.data_root
            / "profiles"
            / "comwechat"
            / "honus.comwechat"
            / "session-events.json"
        )
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
        delivery_stats = delivery_stats_summary(state_root)
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
        reconcile_note = (
            "投递对账：已过期，当前数字按实时队列\n"
            if queue["reconcile_stale"] else ""
        )
        return (
            "EFB 综合状态\n\n"
            f"EFB 运行时间：{format_uptime(self.started_at)}\n"
            f"镜像构建时间：{format_image_build_time(image.get('build_time'))}\n"
            f"运行版本：{runtime_version_text()}\n"
            f"GHCR latest：{format_latest_match(image)}\n"
            f"微信：{self._wechat_login()}\n"
            f"最近退出时间：{format_session_timestamp(session_events.get('last_logout_at'))}\n"
            f"最近登录时间：{format_session_timestamp(session_events.get('last_login_at'))}\n"
            f"Telegram Bot API：{self._bot_api()}\n"
            f"四容器与共享网络：{stack_status}\n"
            f"最近恢复动作：{health.get('action', '暂无')}\n"
            f"自动恢复：{recovery_text}\n"
            f"恢复时段：{recovery_window}\n"
            f"恢复配置：{recovery_config}\n"
            f"失败诊断：{diagnostic_retention}\n"
            f"群成员姓名隐藏：{'开启' if spoiler_enabled else '关闭'}\n"
            f"最近消息活动：{last_delivery}\n"
            f"队列最近延迟：{format_queue_latency(delivery)}\n"
            f"近24小时投递：{format_delivery_stats(delivery_stats)}\n"
            f"投递队列：待处理 {queue['pending']}｜失败 {queue['failed']}\n"
            f"{reconcile_note}"
            f"Bridge 队列：{bridge_summary}\n"
            f"审计：投递 {format_audit_status(reconcile)}｜数据库 {format_audit_status(database)}\n"
            f"容量 {format_audit_status(capacity)}｜上游 {format_audit_status(upstream)}\n"
            f"备份校验：{format_backup_verification(backup_audit)}\n"
            f"维护模式：{format_maintenance_status(maintenance)}\n"
            f"手动重启：{format_manual_restart(manual_restart)}\n"
            f"视频号任务：{_finder_feed_summary(self.channel)}\n"
            f"失败附件已持久化：{queue['persisted_failed_media']} 条\n"
            f"映射数据库：{database_status}\n"
            f"NAS 磁盘剩余：{disk_text}\n"
            f"待评估上游更新：{updates} 项\n"
            f"配置备份：{backup['count']} 份｜{_human_size(backup['bytes'])}\n"
            f"镜像版本：{os.getenv('EFB_IMAGE_REVISION', '未知')}"
        )

    def health(self, update: Update, _context: CallbackContext):
        if self._allowed(update):
            self._send(
                update,
                self.health_text(),
                "status",
                include_bridge=True,
                track_status_source=True,
            )

    def delivery_detail(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        state_root = self.data_root / "operations" / "state"
        delivery = load_json(state_root / "delivery.json")
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        queue = delivery_summary(self.data_root, reconcile)
        pending_records = self._pending_records()
        failed_records = self._failed_records()
        reconcile_note = (
            "投递对账：已过期，当前数字按实时队列\n"
            if queue["reconcile_stale"] else ""
        )
        text = (
            "EFB 投递明细\n\n"
            f"待处理：{queue['pending']} 条\n"
            f"失败：{queue['failed']} 条\n"
            f"{reconcile_note}"
            f"失败附件已持久化：{queue['persisted_failed_media']} 条\n"
            f"最近入站：{format_timestamp(delivery.get('last_inbound_at'))}\n"
            f"最近投递：{format_timestamp(delivery.get('last_delivered_at'))}\n"
            f"最近失败：{format_timestamp(delivery.get('last_failed_at'))}\n\n"
            f"可查看记录：待处理 {len(pending_records)} 条｜失败 {len(failed_records)} 条"
        )
        self._render(update, text, self.delivery_markup(queue["pending"], queue["failed"]))

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

    def restart_all(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        state_path = self.data_root / "operations" / "state" / "manual-restart.json"
        state = request_manual_restart(
            state_path,
            requested_by=getattr(update.effective_user, "id", None),
        )
        if state.get("status") == "requested":
            text = (
                "EFB 全部重启\n\n"
                "已提交手动重启请求。NAS 健康守护会按依赖顺序处理："
                "ComWechat → Bot API 与 watchdog → EFB，并在完成后检查四项健康状态。\n\n"
                "执行期间消息转发会短暂暂停，不会删除微信会话或配置。"
            )
        else:
            text = f"EFB 全部重启\n\n当前已有请求：{format_manual_restart(state)}。"
        self._send(update, text, "status", include_bridge=True)

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
        data = query.data or ""
        if data.startswith("ops:delivery:"):
            self.delivery_callback(update, context)
            return
        action = data.split(":", 1)[-1]
        if action in {"close", "status-close"}:
            query.answer()
            source = self._status_source_messages.pop(
                (query.message.chat.id, query.message.message_id),
                None,
            ) if action == "status-close" else None
            try:
                query.message.delete()
            except Exception as error:
                logger = getattr(self.channel, "logger", None)
                if logger is not None:
                    logger.warning("运维面板消息删除失败: %s", error)
            if source:
                try:
                    self.channel.bot_manager.delete_message(*source)
                except Exception as error:
                    logger = getattr(self.channel, "logger", None)
                    if logger is not None:
                        logger.warning("状态命令消息删除失败: %s", error)
            return
        handlers = {
            "health": self.health,
            "status": self.status,
            "restart-all": self.restart_all,
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
