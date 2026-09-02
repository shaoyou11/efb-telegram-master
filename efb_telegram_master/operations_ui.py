import json
import hashlib
import os
import re
import tempfile
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
from .backup_management import find_backup, list_backups
from .delivery_telemetry import delivery_stats_summary, delivery_trace_summary
from .failed_media import cleanup_failed_media


SENSITIVE_KEY = re.compile(r"(?i)^(token|password|passwd|secret|api_hash|api_id|vncpass)$")
BOT_TOKEN = re.compile(r"bot\d+:[^/\s]+")
URL = re.compile(r"https?://[^\s]+")
DELIVERY_PAGE_SIZE = 5
BACKUP_PAGE_SIZE = 5
RECONCILE_MAX_AGE_SECONDS = 3 * 60 * 60
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
TRACE_STAGE_ORDER = (
    ("bridge_enqueued", "入队"),
    ("attachment_ready", "附件就绪"),
    ("efb_received", "EFB 接收"),
    ("telegram_sent", "开始发送"),
    ("telegram_ack", "Telegram 确认"),
)


def format_trace_durations(timestamps: dict) -> str:
    values = []
    for key, label in TRACE_STAGE_ORDER:
        value = (timestamps or {}).get(key)
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            timestamp = None
        values.append((label, timestamp))
    durations = []
    for (start_label, start), (end_label, end) in zip(values, values[1:]):
        if start is None or end is None:
            durations.append(f"{start_label}→{end_label} 未提供")
        elif end < start:
            durations.append(f"{start_label}→{end_label} 时间异常")
        else:
            durations.append(f"{start_label}→{end_label} {end - start:.2f}秒")
    return "｜".join(durations)


def format_trace_report(bridge_records: list, telegram_records: list, trace_filter: str = "") -> str:
    bridge_stages = {
        "staged": "已暂存", "pending": "等待投递", "inflight": "投递中",
        "acked": "已确认", "dead": "死信", "discarded": "已丢弃",
    }
    telegram_stages = {
        "received": "已接收", "delivered": "已投递",
        "filtered": "已过滤", "failed": "失败",
    }
    merged = {}
    wanted = str(trace_filter or "").strip().lower()
    for record in bridge_records or []:
        trace_id = str(record.get("trace_id") or "").lower()
        if not trace_id or (wanted and not trace_id.startswith(wanted)):
            continue
        merged.setdefault(trace_id, {})["bridge"] = record
    for record in telegram_records or []:
        trace_id = str(record.get("trace_id") or "").lower()
        if not trace_id or (wanted and not trace_id.startswith(wanted)):
            continue
        merged.setdefault(trace_id, {})["telegram"] = record
    if not merged:
        return "EFB 投递追踪\n\n当前没有匹配的最近记录。"
    ordered = sorted(
        merged.items(),
        key=lambda item: max(
            float(item[1].get("bridge", {}).get("received_at") or 0),
            float(item[1].get("telegram", {}).get("at") or 0),
        ),
        reverse=True,
    )[:10]
    lines = ["EFB 投递追踪", "", "仅显示脱敏编号、阶段、类型和时间。", ""]
    for trace_id, stages in ordered:
        bridge = stages.get("bridge", {})
        telegram = stages.get("telegram", {})
        bridge_text = bridge_stages.get(bridge.get("state"), str(bridge.get("state") or "暂无"))
        telegram_text = telegram_stages.get(telegram.get("stage"), str(telegram.get("stage") or "暂无"))
        message_type = str(telegram.get("type") or "未知")
        trace_timestamps = dict(bridge.get("trace_timestamps") or {})
        trace_timestamps.update(telegram.get("trace_timestamps") or {})
        lines.append(
            f"{trace_id} · {message_type}\n"
            f"Bridge {bridge_text} · Telegram {telegram_text}\n"
            f"{format_trace_durations(trace_timestamps)}"
        )
    return "\n\n".join(lines)


def format_issues_report(logged_in: bool, queue: dict, bridge: dict,
                         audits: dict, mapping_ok: bool, delivery_stats: dict = None) -> str:
    issues = []
    if not logged_in:
        issues.append("- 微信未登录")
    pending = int(queue.get("pending", 0) or 0)
    failed = int(queue.get("failed", 0) or 0)
    dead = int(bridge.get("dead_letter_size", 0) or 0)
    if pending:
        issues.append(f"- 投递队列：待处理 {pending} 条")
    if failed:
        issues.append(f"- 投递队列：失败 {failed} 条")
    if dead:
        issues.append(f"- Bridge 死信 {dead} 条")
    for name, report in audits.items():
        if report and report.get("healthy") is False:
            reason = redact_error(report.get("reason") or "检查异常")
            issues.append(f"- {name}：{reason}")
    if not mapping_ok:
        issues.append("- 映射数据库异常")
    failed_labels = {
        "text": "文本", "image": "图片", "video": "视频", "file": "文件",
        "public_account": "公众号", "finder": "视频号", "other": "其他",
    }
    by_type = (delivery_stats or {}).get("by_type") or {}
    failed_types = [
        f"{label} {int((by_type.get(key) or {}).get('failed', 0) or 0)}"
        for key, label in failed_labels.items()
        if int((by_type.get(key) or {}).get("failed", 0) or 0) > 0
    ]
    if failed_types:
        issues.append("- 近24小时失败类型：" + "、".join(failed_types))
    if not issues:
        issues.append("当前没有发现需要处理的异常。")
    return "EFB 异常中心\n\n" + "\n".join(issues)


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


def _revision_text(value) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "none", "null"}:
        return "未提供"
    return text[:12]


def format_component_versions(image: dict, bridge: dict) -> str:
    components = (image or {}).get("components")
    components = components if isinstance(components, dict) else {}

    def component(name: str) -> dict:
        value = components.get(name)
        return value if isinstance(value, dict) else {}

    def component_match(value: dict, fallback: str = "未校验") -> str:
        if isinstance(value.get("latest_match"), bool):
            return "匹配" if value["latest_match"] else "不匹配"
        return str(value.get("latest_status") or fallback)

    efb_build = format_image_build_time((image or {}).get("build_time"))
    efb_match = format_latest_match(image).split("（", 1)[0]
    comwechat = component("comwechat")
    bot_api = component("bot_api")
    watchdog = component("watchdog")
    bridge_build = format_image_build_time(
        (bridge or {}).get("build_time") or comwechat.get("build_time")
    )
    rows = (
        ("EFB", _package_version("ehforwarderbot"),
         os.getenv("EFB_CORE_REVISION"), efb_build, efb_match),
        ("Telegram Master", _package_version("efb-telegram-master"),
         os.getenv("EFB_TELEGRAM_MASTER_REVISION"), efb_build, efb_match),
        ("ComWechat Slave", _package_version("efb-wechat-comwechat-slave"),
         os.getenv("EFB_COMWECHAT_SLAVE_REVISION"), efb_build, efb_match),
        ("HTTP Client", _package_version("python-comwechatrobot-http"),
         os.getenv("EFB_COMWECHAT_HTTP_REVISION"), efb_build, efb_match),
        ("ComWechat", str((bridge or {}).get("comwechat_version") or "未提供"),
         (bridge or {}).get("revision") or comwechat.get("revision"), bridge_build,
         component_match(comwechat, os.getenv("COMWECHAT_GHCR_MATCH", "未校验"))),
        ("Bot API", str(bot_api.get("version") or os.getenv("TELEGRAM_BOT_API_VERSION", "未提供")),
         bot_api.get("revision") or os.getenv("TELEGRAM_BOT_API_REVISION"),
         format_image_build_time(bot_api.get("build_time") or os.getenv("TELEGRAM_BOT_API_BUILD_TIME")),
         component_match(bot_api, os.getenv("TELEGRAM_BOT_API_GHCR_MATCH", "未校验"))),
        ("Watchdog", str(watchdog.get("version") or os.getenv("EFB_WATCHDOG_VERSION", "未提供")),
         watchdog.get("revision") or os.getenv("EFB_WATCHDOG_REVISION"),
         format_image_build_time(watchdog.get("build_time") or os.getenv("EFB_WATCHDOG_BUILD_TIME")),
         component_match(watchdog, os.getenv("EFB_WATCHDOG_GHCR_MATCH", "未校验"))),
    )
    return "\n".join(
        f"  {name}: {version}｜rev {_revision_text(revision)}｜构建 {build}｜{match}"
        for name, version, revision, build, match in rows
    )


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
    def latency_text(value) -> str:
        try:
            if value is None:
                return "暂无"
            latency = float(value)
            return (
                f"{latency:.0f} 毫秒"
                if latency < 1000
                else f"{latency / 1000:.2f} 秒"
            )
        except (TypeError, ValueError):
            return "暂无"

    average_text = latency_text(stats.get("average_latency_ms"))
    p95_text = latency_text(stats.get("p95_latency_ms"))
    sections = [
        f"微信接收 {int(stats.get('inbound', 0) or 0)}｜"
        f"Telegram成功 {int(stats.get('delivered', 0) or 0)}｜"
        f"过滤 {int(stats.get('filtered', 0) or 0)}｜"
        f"静默 {int(stats.get('silent', 0) or 0)}｜"
        f"失败 {int(stats.get('failed', 0) or 0)}｜"
        f"平均延迟 {average_text}｜P95延迟 {p95_text}"
    ]
    sections.append(f"最近成功 {format_timestamp(stats.get('last_success_at'))}")
    labels = {
        "text": "文本",
        "image": "图片",
        "video": "视频",
        "file": "文件",
        "public_account": "公众号",
        "finder": "视频号",
        "other": "其他",
    }
    by_type = stats.get("by_type") or {}
    for key in labels:
        item = by_type.get(key)
        if not isinstance(item, dict) or not any(
            int(item.get(field, 0) or 0)
            for field in ("inbound", "delivered", "filtered", "silent", "failed")
        ):
            continue
        detail = (
            f"{labels[key]} 收{int(item.get('inbound', 0) or 0)}/"
            f"成{int(item.get('delivered', 0) or 0)}/"
            f"滤{int(item.get('filtered', 0) or 0)}/"
            f"默{int(item.get('silent', 0) or 0)}/"
            f"败{int(item.get('failed', 0) or 0)}"
        )
        detail += (
            f"/均{latency_text(item.get('average_latency_ms'))}"
            f"/P95 {latency_text(item.get('p95_latency_ms'))}"
        )
        detail += f"/最近{format_timestamp(item.get('last_success_at'))}"
        sections.append(detail)
    return "｜".join(sections)


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


def format_session_event(events: dict, event: str) -> str:
    key = f"last_{event}_at"
    value = events.get(key) if isinstance(events, dict) else None
    if value not in (None, ""):
        return format_session_timestamp(value)
    state = events.get("current_state") if isinstance(events, dict) else None
    if event == "login" and state == "online":
        return "监测开始前已登录（时间未知）"
    if event == "logout" and state == "offline":
        return "监测开始前已退出（时间未知）"
    return "暂无"


def format_health_action(value) -> str:
    text = str(value or "none").strip().lower()
    labels = {
        "none": "暂无",
        "healthy": "正常",
        "wait": "等待复检",
        "cooldown": "冷却中",
        "hold": "等待 ComWechat 内部恢复",
        "hold_cooldown": "等待 ComWechat 内部恢复（冷却中）",
        "comwechat_recovery_requested": "已请求 ComWechat 容器内恢复",
        "comwechat_recovery_request_failed": "ComWechat 容器内恢复请求失败",
        "restart": "正在准备恢复",
    }
    if text in labels:
        return labels[text]

    scopes = {
        "full": "全部服务",
        "efb": "EFB",
        "telegram": "Telegram Bot API 与 EFB",
        "watchdog": "Watchdog",
        "dependents": "依赖服务",
        "hold": "ComWechat",
    }
    if text.startswith("recovered:"):
        scope = scopes.get(text.split(":", 1)[1], "相关服务")
        return f"已恢复（{scope}）"
    if text.startswith("restart_failed:"):
        scope = scopes.get(text.split(":", 1)[1], "相关服务")
        return f"恢复失败（{scope}）"
    return _clean_text(value, 60)


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
    value = None
    if isinstance(metadata, dict):
        value = metadata.get("latest_match")
        if value is None:
            value = metadata.get("ghcr_latest_match")
    if isinstance(value, bool):
        status = "匹配" if value else "不匹配"
    elif str(value).lower() in {"true", "yes", "1", "match", "matched", "匹配"}:
        status = "匹配"
    elif str(value).lower() in {"false", "no", "0", "mismatch", "unmatched", "不匹配"}:
        status = "不匹配"
    else:
        status = "未校验"
    checked = format_timestamp(metadata.get("checked_at")) if isinstance(metadata, dict) else "暂无"
    return f"{status}（最近校验{checked}）"


def format_compact_status(snapshot: dict) -> str:
    return (
        "EFB 综合状态（精简）\n\n"
        f"运行时间：{snapshot.get('uptime', '暂无')}\n"
        f"运行版本：{snapshot.get('versions', '未知')}\n"
        f"GHCR latest：{snapshot.get('latest', '未校验')}\n"
        f"微信：{snapshot.get('wechat', '未知')}\n"
        f"Telegram Bot API：{snapshot.get('bot_api', '未知')}\n"
        f"四容器与共享网络：{snapshot.get('stack', '未知')}\n"
        f"投递队列：{snapshot.get('queue', '未知')}\n"
        f"Bridge 队列：{snapshot.get('bridge', '未知')}\n"
        f"队列最近延迟：{snapshot.get('latency', '暂无')}\n"
        f"最近恢复动作：{snapshot.get('recovery', '暂无')}\n"
        f"备份校验：{snapshot.get('backup', '未检查')}\n"
        f"恢复演练：{snapshot.get('restore', '未检查')}\n"
        f"维护模式：{snapshot.get('maintenance', '关闭')}"
    )


def format_selftest_report(checks: List[dict]) -> str:
    checks = checks if isinstance(checks, list) else []
    status_names = {"ok": "通过", "failed": "异常", "unknown": "未检查"}
    statuses = [str(item.get("status", "unknown")) for item in checks]
    if "failed" in statuses:
        overall = "异常"
    elif "unknown" in statuses:
        overall = "未完成"
    else:
        overall = "通过"
    lines = [
        "EFB 深度自检",
        "",
        f"总体结果：{overall}",
        "只读检查，不发送测试消息、不修改队列、不标记微信已读。",
        "",
    ]
    for item in checks:
        name = _clean_text(item.get("name"), 40)
        status = status_names.get(str(item.get("status")), "未检查")
        detail = _clean_text(item.get("detail"), 120)
        lines.append(f"{name}：{status}（{detail}）")
    if not checks:
        lines.append("当前没有可执行的检查项。")
    return "\n".join(lines)


def format_contact_center(snapshot: dict) -> str:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    unresolved = snapshot.get("unresolved") if isinstance(snapshot.get("unresolved"), list) else []
    aliased = snapshot.get("aliased") if isinstance(snapshot.get("aliased"), list) else []
    lines = [
        "EFB 未识别联系人",
        "",
        f"未识别：{len(unresolved)} 条｜本地别名：{len(aliased)} 条",
    ]
    refresh = snapshot.get("refresh") if isinstance(snapshot.get("refresh"), dict) else None
    if refresh:
        lines.append(
            "刷新完成："
            f"{format_timestamp(refresh.get('completed_at'))}｜"
            f"尝试 {int(refresh.get('attempted', 0) or 0)}｜"
            f"新识别 {int(refresh.get('resolved', 0) or 0)}｜"
            f"剩余 {int(refresh.get('remaining', len(unresolved)) or 0)}"
        )
    for index, item in enumerate(unresolved, start=1):
        history = item.get("history") if isinstance(item.get("history"), list) else []
        lines.extend([
            "",
            f"未识别 #{index}",
            f"类型：{_clean_text(item.get('kind'), 20)}",
            f"标识：{_clean_text(item.get('uid'), 80)}",
            f"当前名称：{_clean_text(item.get('name'), 80)}",
            f"历史名称：{_clean_text('、'.join(str(name) for name in history[-5:]), 120)}",
        ])
    for index, item in enumerate(aliased, start=1):
        history = item.get("history") if isinstance(item.get("history"), list) else []
        lines.extend([
            "",
            f"本地别名 #{index}",
            f"类型：{_clean_text(item.get('kind'), 20)}",
            f"标识：{_clean_text(item.get('uid'), 80)}",
            f"本地别名：{_clean_text(item.get('alias'), 80)}",
            f"历史名称：{_clean_text('、'.join(str(name) for name in history[-5:]), 120)}",
        ])
    if not unresolved and not aliased:
        lines.extend(["", "当前没有未识别联系人。"])
    lines.extend([
        "",
        "设置别名：/contact_alias <标识> <名称>",
        "清除别名：/contact_alias <标识> -",
    ])
    return "\n".join(lines)


def format_restore_rehearsal_status(state: dict) -> str:
    if not isinstance(state, dict) or not state:
        return "未检查"
    status = str(state.get("status") or "").lower()
    if status == "requested":
        return "等待执行"
    if status == "running":
        return "执行中"
    if status == "completed" and state.get("healthy"):
        return f"通过（最近完成 {format_timestamp(state.get('completed_at'))}）"
    if status in {"completed", "failed"}:
        reason = _clean_text(state.get("reason") or "恢复演练未通过", 80)
        return f"失败（{reason}）"
    return "未检查"


def request_restore_rehearsal(path: Path, now=None, requested_by=None) -> dict:
    path = Path(path)
    existing = load_json(path)
    if existing.get("status") in {"requested", "running"}:
        return existing
    now = time.time() if now is None else float(now)
    payload = {
        "version": 1,
        "request_id": f"restore-{int(now * 1000)}",
        "status": "requested",
        "requested_at": now,
    }
    if requested_by is not None:
        payload["requested_by"] = int(requested_by)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return payload


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
    def markup(
        refresh: str = "",
        include_bridge: bool = False,
        detailed: bool = False,
        wechat_read_enabled: bool = False,
        digest_enabled: bool = False,
    ) -> InlineKeyboardMarkup:
        rows = []
        if include_bridge:
            rows.append([
                InlineKeyboardButton(
                    "精简状态" if detailed else "详细状态",
                    callback_data="ops:status" if detailed else "ops:status-detail",
                ),
                InlineKeyboardButton("投递明细", callback_data="ops:delivery"),
                InlineKeyboardButton("异常中心", callback_data="ops:errors"),
            ])
            rows.append([
                InlineKeyboardButton("深度自检", callback_data="ops:selftest"),
                InlineKeyboardButton("联系人中心", callback_data="ops:contacts"),
                InlineKeyboardButton("失败诊断", callback_data="ops:diagnostic"),
            ])
            row = [InlineKeyboardButton("Bridge 队列", callback_data="bridgeq:home")]
            row.append(InlineKeyboardButton("全部重启", callback_data="ops:restart-all"))
            if refresh:
                row.append(InlineKeyboardButton("刷新", callback_data=f"ops:{refresh}"))
            rows.append(row)
            rows.append([
                InlineKeyboardButton(
                    "微信自动已读：开" if wechat_read_enabled else "微信自动已读：关",
                    callback_data="ops:wechat-read-toggle",
                ),
                InlineKeyboardButton(
                    "静默摘要：开" if digest_enabled else "静默摘要：关",
                    callback_data="ops:digest-toggle",
                ),
            ])
            rows.append([
                InlineKeyboardButton("恢复演练", callback_data="ops:restore-rehearsal"),
            ])
            rows.append([
                InlineKeyboardButton("关闭并删除", callback_data="ops:status-close"),
            ])
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
        detailed: bool = False,
    ):
        wechat_read = getattr(self.channel, "wechat_read_ui", None)
        digest = getattr(self.channel, "digest_guard", None)
        markup = self.markup(
            refresh,
            include_bridge=include_bridge,
            detailed=detailed,
            wechat_read_enabled=bool(getattr(wechat_read, "enabled", False)),
            digest_enabled=bool(getattr(digest, "enabled", False)),
        )
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

    def _bridge_health(self) -> dict:
        bridge_ui = getattr(self, "bridge_queue_ui", None)
        client = getattr(bridge_ui, "client", None)
        if client is None:
            return {}
        try:
            return client.health()
        except Exception:
            return {}

    def _bridge_queue_summary(self, snapshot: dict = None) -> str:
        try:
            snapshot = snapshot if isinstance(snapshot, dict) else self._bridge_health()
            if not snapshot:
                return "检测失败"
            staged = int(snapshot.get("staged_size", 0) or 0)
            pending = int(snapshot.get("pending_size", 0) or 0)
            inflight = int(snapshot.get("inflight_size", 0) or 0)
            total = int(snapshot.get("queue_size", staged + pending + inflight) or 0)
            dead = int(snapshot.get("dead_letter_size", 0) or 0)
            return f"暂存 {staged}｜待投递 {pending}｜处理中 {inflight}｜总计 {total}｜死信 {dead}"
        except Exception as error:
            return f"不可用（{redact_error(error)}）"

    def _compact_status_snapshot(self) -> dict:
        state_root = self.data_root / "operations" / "state"
        delivery = load_json(state_root / "delivery.json")
        image = image_metadata(self.data_root)
        health = load_json(state_root / "health-guard.json")
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        backup_audit = load_json(self.data_root / "backup-audit-latest.json")
        maintenance = load_json(state_root / "maintenance.json")
        restore = load_json(state_root / "restore-rehearsal.json")
        queue = delivery_summary(self.data_root, reconcile)
        return {
            "uptime": format_uptime(self.started_at),
            "versions": runtime_version_text(),
            "latest": format_latest_match(image),
            "wechat": self._wechat_login(),
            "bot_api": self._bot_api(),
            "stack": "正常" if health.get("healthy") else health.get("reason", "等待检查"),
            "queue": f"待处理 {queue['pending']}｜失败 {queue['failed']}",
            "bridge": self._bridge_queue_summary(),
            "latency": format_queue_latency(delivery),
            "recovery": format_health_action(health.get("action")),
            "backup": format_backup_verification(backup_audit),
            "restore": format_restore_rehearsal_status(restore),
            "maintenance": format_maintenance_status(maintenance),
        }

    def compact_health_text(self) -> str:
        return format_compact_status(self._compact_status_snapshot())

    def _selftest_checks(self) -> List[dict]:
        checks = []

        try:
            login = self._wechat_login()
            checks.append({
                "name": "微信登录",
                "status": "ok" if login == "已登录" else "failed",
                "detail": login,
            })
        except Exception as error:
            checks.append({"name": "微信登录", "status": "failed", "detail": redact_error(error)})

        try:
            bot_api = self._bot_api()
            checks.append({
                "name": "Telegram Bot API",
                "status": "ok" if bot_api == "正常" else "failed",
                "detail": bot_api,
            })
        except Exception as error:
            checks.append({
                "name": "Telegram Bot API",
                "status": "failed",
                "detail": redact_error(error),
            })

        bridge = getattr(getattr(self, "bridge_queue_ui", None), "client", None)
        if bridge is None:
            checks.append({"name": "Bridge 接口", "status": "unknown", "detail": "未配置"})
        else:
            try:
                snapshot = bridge.health()
                checks.append({
                    "name": "Bridge 接口",
                    "status": "ok" if snapshot.get("ok", True) is not False else "failed",
                    "detail": "接口正常" if snapshot.get("ok", True) is not False else "接口返回异常",
                })
            except Exception as error:
                checks.append({
                    "name": "Bridge 接口",
                    "status": "failed",
                    "detail": redact_error(error),
                })

        state_root = self.data_root / "operations" / "state"
        delivery_path = state_root / "delivery.json"
        if delivery_path.is_file() and load_json(delivery_path):
            checks.append({"name": "投递状态文件", "status": "ok", "detail": "可读"})
        else:
            checks.append({"name": "投递状态文件", "status": "unknown", "detail": "尚未生成"})

        for label, filename in (
            ("数据库审计", "database-audit-latest.json"),
            ("备份校验", "backup-audit-latest.json"),
        ):
            report = load_json(self.data_root / filename)
            if not report:
                checks.append({"name": label, "status": "unknown", "detail": "未检查"})
            else:
                checks.append({
                    "name": label,
                    "status": "ok" if report.get("healthy") else "failed",
                    "detail": format_audit_status(report),
                })

        failed_media = self.data_root / "operations" / "failed-media"
        failed_count = delivery_summary(
            self.data_root,
            load_json(self.data_root / "delivery-reconcile-latest.json"),
        )["persisted_failed_media"]
        checks.append({
            "name": "失败附件目录",
            "status": "ok" if failed_count == 0 or failed_media.is_dir() else "failed",
            "detail": "无待处理副本" if failed_count == 0 else f"可访问，{failed_count} 条",
        })
        return checks

    @staticmethod
    def contact_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("刷新联系人", callback_data="ops:contacts-refresh")],
            [InlineKeyboardButton("关闭", callback_data="ops:close")],
        ])

    def _contact_snapshot(self, refresh: bool = False):
        slave = self._comwechat_channel()
        method_name = "refresh_contact_center" if refresh else "contact_center_snapshot"
        method = getattr(slave, method_name, None) if slave is not None else None
        if not callable(method):
            return None
        return method()

    def contact_center(self, update: Update, _context: CallbackContext, refresh: bool = False):
        if not self._allowed(update):
            return
        if refresh:
            self._render(
                update,
                "EFB 未识别联系人\n\n正在刷新联系人，请稍候……",
                self.contact_markup(),
            )
        try:
            snapshot = self._contact_snapshot(refresh=refresh)
        except Exception as error:
            self._render(
                update,
                "EFB 未识别联系人\n\n刷新失败：" + redact_error(error),
                self.contact_markup(),
            )
            return
        if snapshot is None:
            text = "EFB 未识别联系人\n\n当前 ComWechat 版本不支持联系人中心。"
        else:
            text = format_contact_center(snapshot)
        self._render(update, text, self.contact_markup())

    def contacts(self, update: Update, context: CallbackContext):
        self.contact_center(update, context)

    def contact_alias(self, update: Update, context: CallbackContext):
        if not self._allowed(update):
            return
        args = list(getattr(context, "args", None) or [])
        if len(args) < 2:
            self._render(
                update,
                "EFB 联系人别名\n\n用法：/contact_alias <标识> <名称>\n清除别名：/contact_alias <标识> -",
                self.contact_markup(),
            )
            return
        wxid = args[0]
        try:
            slave = self._comwechat_channel()
            if args[1] == "-" and callable(getattr(slave, "clear_contact_alias", None)):
                snapshot = slave.clear_contact_alias(wxid)
                message = "已清除本地别名。"
            elif callable(getattr(slave, "set_contact_alias", None)):
                snapshot = slave.set_contact_alias(wxid, " ".join(args[1:]))
                message = "已设置本地别名。"
            else:
                raise RuntimeError("当前 ComWechat 版本不支持联系人别名")
        except Exception as error:
            self._render(update, "EFB 联系人别名\n\n操作失败：" + redact_error(error), self.contact_markup())
            return
        self._render(update, "EFB 联系人别名\n\n" + message + "\n\n" + format_contact_center(snapshot), self.contact_markup())

    def selftest(self, update: Update, _context: CallbackContext):
        if self._allowed(update):
            self._send(update, format_selftest_report(self._selftest_checks()), "selftest")

    def restore_rehearsal(self, update: Update, _context: CallbackContext):
        if not self._allowed(update):
            return
        state_path = self.data_root / "operations" / "state" / "restore-rehearsal-request.json"
        state = request_restore_rehearsal(
            state_path,
            requested_by=getattr(update.effective_user, "id", None),
        )
        if state.get("status") == "requested":
            text = (
                "EFB 备份恢复演练\n\n"
                "已提交只读演练请求。NAS 健康守护会在临时目录校验清单、SQLite 和加密归档，"
                "不会覆盖生产配置。完成后刷新 /status 查看结果。"
            )
        else:
            text = f"EFB 备份恢复演练\n\n当前状态：{format_restore_rehearsal_status(state)}。"
        self._send(update, text, "status", include_bridge=True)

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
        restore_rehearsal = load_json(state_root / "restore-rehearsal.json")
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
        image_perception = getattr(self.channel, "image_perception", None)
        image_perception_text = image_perception.summary() if image_perception else "关闭"
        wechat_read = getattr(self.channel, "wechat_read_ui", None)
        wechat_read_text = wechat_read.summary() if wechat_read else "未启用"
        last_delivery = format_timestamp(
            delivery.get("last_delivered_at") or delivery.get("last_inbound_at")
        )
        stack_status = "正常" if health.get("healthy") else health.get("reason", "等待首次检查")
        database_status = "正常" if database.get("healthy") else "等待检查或异常"
        disk = capacity.get("disk") or {}
        free_percent = disk.get("free_percent")
        disk_text = f"{float(free_percent):.2f}%" if isinstance(free_percent, (int, float)) else "等待检查"
        queue = delivery_summary(self.data_root, reconcile)
        bridge_health = self._bridge_health()
        bridge_summary = self._bridge_queue_summary(bridge_health)
        component_versions = format_component_versions(image, bridge_health)
        delivery_stats = delivery_stats_summary(state_root)
        version_lines = "\n".join(
            f"  {item}" for item in runtime_version_text().split("｜")
        )
        delivery_stats_lines = "\n".join(
            f"  {item}" for item in format_delivery_stats(delivery_stats).split("｜")
        )
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
                f"检查间隔：{_duration_text(watchdog.get('poll_seconds', 120))}\n"
                f"点击冷却：{_duration_text(watchdog.get('click_cooldown_seconds', 120))}\n"
                f"连续失败：{watchdog.get('max_recovery_failures', 3)}次后暂停"
            )
            login_protection = (
                "扫码保护"
                + ("开启" if watchdog.get("manual_login_protection") else "关闭")
                + "｜启动保护"
                + _duration_text(watchdog.get("startup_grace_seconds", 90))
            )
            diagnostic_retention = watchdog.get("diagnostic_retention", "仅保留最新一张")
        else:
            recovery_text = "等待检查"
            recovery_window = "等待检查"
            recovery_config = "等待检查"
            login_protection = "等待检查"
            diagnostic_retention = "等待检查"
        recovery_config_lines = "\n".join(
            f"  {item}" for item in recovery_config.splitlines()
        )
        reconcile_note = (
            "投递对账：已过期，当前数字按实时队列\n"
            if queue["reconcile_stale"] else ""
        )
        return (
            "EFB 综合状态\n\n"
            "【运行环境】\n"
            f"运行时间：{format_uptime(self.started_at)}\n"
            f"镜像构建：{format_image_build_time(image.get('build_time'))}\n"
            f"运行版本：\n{version_lines}\n"
            f"组件状态：\n{component_versions}\n"
            f"GHCR latest：{format_latest_match(image)}\n"
            "\n【微信与自动恢复】\n"
            f"微信状态：{self._wechat_login()}\n"
            f"最近登录：{format_session_event(session_events, 'login')}\n"
            f"最近退出：{format_session_event(session_events, 'logout')}\n"
            f"Telegram Bot API：{self._bot_api()}\n"
            f"四容器与共享网络：{stack_status}\n"
            f"最近恢复动作：{format_health_action(health.get('action'))}\n"
            f"自动恢复：{recovery_text}\n"
            f"恢复时段：{recovery_window}\n"
            f"恢复配置：\n{recovery_config_lines}\n"
            f"登录保护：{login_protection}\n"
            f"失败诊断：{diagnostic_retention}\n"
            f"群成员姓名隐藏：{'开启' if spoiler_enabled else '关闭'}\n"
            f"图片感知：{image_perception_text}\n"
            f"微信自动已读：{wechat_read_text}\n"
            "\n【消息投递】\n"
            f"最近消息活动：{last_delivery}\n"
            f"队列最近延迟：{format_queue_latency(delivery)}\n"
            f"近24小时投递：\n{delivery_stats_lines}\n"
            f"投递队列：待处理 {queue['pending']}｜失败 {queue['failed']}\n"
            f"{reconcile_note}"
            f"Bridge 队列：{bridge_summary}\n"
            f"视频号任务：{_finder_feed_summary(self.channel)}\n"
            f"失败附件已持久化：{queue['persisted_failed_media']} 条\n"
            "\n【巡检与存储】\n"
            f"投递审计：{format_audit_status(reconcile)}\n"
            f"数据库审计：{format_audit_status(database)}\n"
            f"容量审计：{format_audit_status(capacity)}\n"
            f"上游审计：{format_audit_status(upstream)}\n"
            f"备份校验：{format_backup_verification(backup_audit)}\n"
            f"恢复演练：{format_restore_rehearsal_status(restore_rehearsal)}\n"
            f"维护模式：{format_maintenance_status(maintenance)}\n"
            f"手动重启：{format_manual_restart(manual_restart)}\n"
            f"映射数据库：{database_status}\n"
            f"NAS 磁盘剩余：{disk_text}\n"
            f"待评估上游更新：{updates} 项\n"
            f"配置备份：{backup['count']} 份｜{_human_size(backup['bytes'])}\n"
            "\n【版本标识】\n"
            f"{os.getenv('EFB_IMAGE_REVISION', '未知')}"
        )

    def health(self, update: Update, _context: CallbackContext):
        if self._allowed(update):
            self._send(
                update,
                self.health_text(),
                "status",
                include_bridge=True,
                track_status_source=True,
                detailed=True,
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
        reconcile = load_json(self.data_root / "delivery-reconcile-latest.json")
        queue = delivery_summary(self.data_root, reconcile)
        try:
            bridge = self.bridge_queue_ui.client.health()
        except Exception:
            bridge = {"dead_letter_size": 0}
        database_audit = load_json(self.data_root / "database-audit-latest.json")
        audits = {
            "投递审计": reconcile,
            "数据库审计": database_audit,
            "容量审计": load_json(self.data_root / "capacity-audit-latest.json"),
            "备份校验": load_json(self.data_root / "backup-audit-latest.json"),
        }
        mapping_ok = bool(database_audit.get("healthy"))
        try:
            logged_in = self._wechat_login() == "已登录"
        except Exception:
            logged_in = False
        self._send(
            update,
            format_issues_report(
                logged_in,
                queue,
                bridge,
                audits,
                mapping_ok,
                delivery_stats_summary(state_root),
            ),
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
        if self._allowed(update):
            self._send(
                update,
                self.compact_health_text(),
                "status",
                include_bridge=True,
                track_status_source=True,
            )

    def status_detail(self, update: Update, context: CallbackContext):
        self.health(update, context)

    def trace(self, update: Update, context: CallbackContext):
        if not self._allowed(update):
            return
        bridge_records = []
        try:
            bridge_records = self.bridge_queue_ui.client.trace(30)
        except Exception:
            pass
        trace_filter = context.args[0] if getattr(context, "args", None) else ""
        telegram_records = delivery_trace_summary(self.data_root / "operations" / "state")
        self._send(
            update,
            format_trace_report(bridge_records, telegram_records, trace_filter),
            "trace",
            include_bridge=True,
        )

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
        self._show_backup_list(update, 0)

    def _backup_records(self) -> list:
        restore = load_json(
            self.data_root / "operations" / "state" / "restore-rehearsal.json"
        )
        restore_source = str(
            restore.get("backup") or restore.get("source") or ""
        )
        return list_backups(self.data_root / "backups", restore_source)

    @staticmethod
    def _backup_list_markup(records: list, page: int) -> InlineKeyboardMarkup:
        start = page * BACKUP_PAGE_SIZE
        visible = records[start:start + BACKUP_PAGE_SIZE]
        rows = [
            [InlineKeyboardButton(
                item["name"], callback_data=f"ops:backup:view:{item['name']}"
            )]
            for item in visible
        ]
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "上一页", callback_data=f"ops:backup:list:{page - 1}"
            ))
        if start + BACKUP_PAGE_SIZE < len(records):
            navigation.append(InlineKeyboardButton(
                "下一页", callback_data=f"ops:backup:list:{page + 1}"
            ))
        if navigation:
            rows.append(navigation)
        rows.append([
            InlineKeyboardButton("刷新", callback_data=f"ops:backup:list:{page}"),
            InlineKeyboardButton("关闭", callback_data="ops:close"),
        ])
        return InlineKeyboardMarkup(rows)

    def _show_backup_list(self, update: Update, page: int) -> None:
        records = self._backup_records()
        max_page = max(0, (len(records) - 1) // BACKUP_PAGE_SIZE)
        page = max(0, min(int(page), max_page))
        total = sum(int(item.get("bytes", 0) or 0) for item in records)
        visible = records[page * BACKUP_PAGE_SIZE:(page + 1) * BACKUP_PAGE_SIZE]
        lines = [
            "EFB 配置备份",
            "",
            f"数量：{len(records)} 份｜占用：{_human_size(total)}",
            f"页码：{page + 1}/{max_page + 1}",
            "",
        ]
        if visible:
            for item in visible:
                protection = "、".join(item["protected"]) or "可人工删除"
                lines.append(
                    f"{item['name']}\n"
                    f"  {_human_size(item['bytes'])}｜清单 {item['manifest']}｜"
                    f"SQLite {item['sqlite']}｜{protection}"
                )
        else:
            lines.append("当前没有配置备份。")
        lines.extend(["", "仅显示校验结果，不读取或传输配置正文。"])
        markup = self._backup_list_markup(records, page)
        if update.callback_query:
            update.callback_query.edit_message_text("\n".join(lines), reply_markup=markup)
        else:
            update.effective_message.reply_text("\n".join(lines), reply_markup=markup)

    def _show_backup_detail(self, update: Update, name: str) -> None:
        record = find_backup(self._backup_records(), name)
        if not record:
            text = "EFB 备份详情\n\n该备份不存在或已移动。"
        else:
            protection = "、".join(record["protected"]) or "无"
            text = (
                "EFB 备份详情\n\n"
                f"名称：{record['name']}\n"
                f"创建：{format_timestamp(record['created_at'])}\n"
                f"大小：{_human_size(record['bytes'])}\n"
                f"文件清单：{record['manifest']}\n"
                f"SQLite：{record['sqlite']}\n"
                f"保护原因：{protection}\n"
                "删除方式：现有备份为多文件目录，按安全约束仅允许在 NAS 人工删除。"
            )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("返回", callback_data="ops:backup:list:0"),
            InlineKeyboardButton("关闭", callback_data="ops:close"),
        ]])
        update.callback_query.edit_message_text(text, reply_markup=markup)

    def backup_callback(self, update: Update) -> None:
        query = update.callback_query
        parts = (query.data or "").split(":", 3)
        if len(parts) != 4:
            query.answer("无效操作", show_alert=True)
            return
        query.answer()
        if parts[2] == "list":
            try:
                page = int(parts[3])
            except ValueError:
                page = 0
            self._show_backup_list(update, page)
        elif parts[2] == "view":
            self._show_backup_detail(update, parts[3])
        else:
            query.answer("无效操作", show_alert=True)

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
        if data.startswith("ops:backup:"):
            self.backup_callback(update)
            return
        action = data.split(":", 1)[-1]
        if action == "wechat-read-toggle":
            settings = getattr(self.channel, "wechat_read_ui", None)
            if settings is None:
                query.answer("微信自动已读当前不可用", show_alert=True)
                return
            settings.set_enabled(not settings.enabled)
            query.answer("微信自动已读已开启" if settings.enabled else "微信自动已读已关闭")
            self.status(update, context)
            return
        if action == "digest-toggle":
            digest = getattr(self.channel, "digest_guard", None)
            if digest is None:
                query.answer("静默投递摘要当前不可用", show_alert=True)
                return
            digest.set_enabled(not digest.enabled)
            query.answer("静默投递摘要已开启" if digest.enabled else "静默投递摘要已关闭")
            self.status(update, context)
            return
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
            "status-detail": self.status_detail,
            "restart-all": self.restart_all,
            "delivery": self.delivery_detail,
            "errors": self.errors,
            "diagnostic": self.diagnostic,
            "selftest": self.selftest,
            "contacts": self.contacts,
            "contacts-refresh": lambda current_update, current_context: self.contact_center(
                current_update, current_context, refresh=True
            ),
            "restore-rehearsal": self.restore_rehearsal,
            "trace": self.trace,
            "backup": self.backup_info,
            "filetest": self.filetest,
            "security": self.security,
        }
        if action == "contacts-refresh":
            query.answer("已开始刷新联系人，请稍候……")
        else:
            query.answer()
        handler = handlers.get(action)
        if handler:
            handler(update, context)
