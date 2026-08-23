import json
import os
from types import SimpleNamespace
from pathlib import Path

from efb_telegram_master.operations_ui import (
    OperationsUI,
    _human_size,
    backup_summary,
    clear_invalid_delivery_records,
    delivery_details,
    delivery_summary,
    format_timestamp,
    format_uptime,
    format_delivery_details,
    load_json,
    redact_error,
    scan_sensitive_keys,
)


def test_load_json_rejects_invalid_content(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("invalid", encoding="utf-8")
    assert load_json(path) == {}


def test_format_timestamp_handles_missing_value():
    assert format_timestamp(None) == "暂无"


def test_format_uptime_formats_process_runtime():
    assert format_uptime(1000, now=4661) == "1小时 1分钟"


def test_status_text_summarizes_persistent_reports(tmp_path, monkeypatch):
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    (tmp_path / "backups/config-1").mkdir(parents=True)
    (state / "delivery.json").write_text(json.dumps({
        "last_delivered_at": 1000,
    }), encoding="utf-8")
    (state / "health-guard.json").write_text(json.dumps({
        "healthy": True,
        "action": "healthy",
    }), encoding="utf-8")
    (tmp_path / "delivery-reconcile-latest.json").write_text(json.dumps({
        "pending_count": 1,
        "failed_count": 2,
    }), encoding="utf-8")
    (tmp_path / "database-audit-latest.json").write_text(json.dumps({
        "healthy": True,
    }), encoding="utf-8")
    (tmp_path / "capacity-audit-latest.json").write_text(json.dumps({
        "disk": {"free_percent": 75.5},
    }), encoding="utf-8")
    (tmp_path / "upstream-audit-latest.json").write_text(json.dumps({
        "update_count": 3,
    }), encoding="utf-8")

    ui = OperationsUI.__new__(OperationsUI)
    ui.data_root = Path(tmp_path)
    ui.started_at = 1000
    ui.channel = SimpleNamespace(
        author_name_spoiler_store=SimpleNamespace(enabled=True),
        watchdog_control=SimpleNamespace(get_state=lambda: {
            "master_enabled": True,
            "event_enabled": True,
            "night_enabled": False,
        }),
    )
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 4661)

    text = ui.health_text()

    assert "EFB 综合状态" in text
    assert "待处理 1｜失败 2" in text
    assert "NAS 磁盘剩余：75.50%" in text
    assert "待评估上游更新：3 项" in text
    assert "EFB 运行时间：1小时 1分钟" in text
    assert "群成员姓名隐藏：开启" in text
    assert "自动恢复：总开关开启｜全天开启｜凌晨关闭" in text


def test_status_text_shows_platform_sync_backup_and_queue_health(tmp_path, monkeypatch):
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    (tmp_path / "deployment-manifest.json").write_text(json.dumps({
        "platform": "feiniu-t640",
        "compose_project": "efb2026",
    }), encoding="utf-8")
    (state / "image-metadata.json").write_text(json.dumps({
        "build_time": "2026-08-23T01:02:03Z",
        "revision": "efb-test-revision",
        "latest_match": True,
    }), encoding="utf-8")
    (tmp_path / "config-drift-latest.json").write_text(json.dumps({
        "healthy": True,
        "issues": [],
    }), encoding="utf-8")
    (tmp_path / "backup-audit-latest.json").write_text(json.dumps({
        "healthy": True,
        "latest_backup": "config-1",
    }), encoding="utf-8")
    (tmp_path / "delivery-reconcile-latest.json").write_text(json.dumps({
        "pending_count": 0,
        "failed_count": 0,
        "oldest_pending_seconds": 0,
    }), encoding="utf-8")

    ui = OperationsUI.__new__(OperationsUI)
    ui.data_root = Path(tmp_path)
    ui.started_at = 1000
    ui.channel = SimpleNamespace()
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 1000)

    text = ui.health_text()

    assert "部署平台：feiniu-t640" in text
    assert "配置同步：正常" in text
    assert "镜像构建时间：2026-08-23T01:02:03Z" in text
    assert "GHCR latest：匹配" in text
    assert "队列最近延迟：0秒" in text
    assert "备份校验：正常" in text


def test_status_falls_back_to_persistent_delivery_queues(tmp_path, monkeypatch):
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    pending_path = tmp_path / "profiles/comwechat/honus.comwechat"
    pending_path.mkdir(parents=True)
    (pending_path / "pending-files.json").write_text(json.dumps({
        "pending-token": {"path": "/data/file"},
    }), encoding="utf-8")
    (state / "failed-deliveries.json").write_text(json.dumps({
        "failed-token": {"storage": "durable", "path": "/data/operations/failed-media/failed-token/file"},
    }), encoding="utf-8")

    ui = OperationsUI.__new__(OperationsUI)
    ui.data_root = Path(tmp_path)
    ui.started_at = 1000
    ui.channel = SimpleNamespace()
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 1000)

    text = ui.health_text()

    assert "待处理 1｜失败 1" in text
    assert "失败附件已持久化：1 条" in text


def test_status_uses_live_queue_counts_after_reconcile_report_is_stale(tmp_path, monkeypatch):
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    pending_path = tmp_path / "profiles/comwechat/honus.comwechat"
    pending_path.mkdir(parents=True)
    (pending_path / "pending-files.json").write_text("{}", encoding="utf-8")
    (state / "failed-deliveries.json").write_text("{}", encoding="utf-8")
    (tmp_path / "delivery-reconcile-latest.json").write_text(json.dumps({
        "pending_count": 0,
        "failed_count": 2,
    }), encoding="utf-8")

    ui = OperationsUI.__new__(OperationsUI)
    ui.data_root = Path(tmp_path)
    ui.started_at = 1000
    ui.channel = SimpleNamespace()
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 1000)

    text = ui.health_text()

    assert "待处理 0｜失败 0" in text


def test_delivery_details_exposes_pending_and_failed_items(tmp_path):
    pending_path = tmp_path / "profiles/comwechat/honus.comwechat"
    pending_path.mkdir(parents=True)
    (pending_path / "pending-files.json").write_text(json.dumps({
        "/comwechat/Files/": {
            "author_alias": "耶巴蒂",
            "author_name": "最幸福的事",
            "chat_name": "子擎玩家交流群2",
            "chat_kind": "group",
            "msg": {
                "type": "video",
                "msgid": "5626155171855065761",
                "timestamp": 1900,
                "text": "待处理视频",
            },
        },
    }), encoding="utf-8")

    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    available = tmp_path / "available.jpg"
    available.write_bytes(b"image")
    (state / "failed-deliveries.json").write_text(json.dumps({
        "available-token": {
            "uid": "1234567890",
            "type": "Image",
            "path": str(available),
            "filename": "available.jpg",
            "error": "Flood control exceeded",
            "created_at": 1900,
            "expires": 3000,
            "chat": "honus.comwechat 48182341636@chatroom",
            "author_uid": "wxid_sender",
            "tg_dest": -1001234567890,
            "thread_id": 22,
            "text": "完整失败消息内容",
        },
        "missing-token": {
            "uid": "9876543210",
            "type": "Video",
            "path": str(tmp_path / "missing.mp4"),
            "filename": "missing.mp4",
            "error": "source unavailable",
            "created_at": 1900,
            "expires": 3000,
        },
    }), encoding="utf-8")

    details = delivery_details(tmp_path, now=2000)
    text = format_delivery_details(tmp_path, now=2000)

    assert len(details["pending"]) == 1
    assert details["pending"][0]["can_retry"] is False
    assert "待处理 #1" in text
    assert "类型：视频" in text
    assert "文件：未记录" in text
    assert "微信会话：子擎玩家交流群2" in text
    assert "发送者：最幸福的事（耶巴蒂）" in text
    assert "消息：5626155171855065761" in text
    assert "内容：待处理视频" in text
    assert "无法继续" in text
    assert "失败 #1" in text
    assert "原因：Flood control exceeded" in text
    assert "微信会话：honus.comwechat 48182341636@chatroom" in text
    assert "发送者：wxid_sender" in text
    assert "发送到：Telegram chat_id=-1001234567890，话题=22" in text
    assert "消息：1234567890" in text
    assert "内容：完整失败消息内容" in text
    assert "失败 #2" in text
    assert "原文件已不存在，无法继续推送" in text


def test_delivery_details_exposes_bridge_active_items(tmp_path):
    active = [{
        "id": "bridge-article",
        "state": "staged",
        "received_at": 1900,
        "available_at": 1900,
        "attempts": 0,
        "last_error": "",
        "source_key": "48182341636@chatroom",
        "message": {
            "type": 49,
            "msgid": "article-1900",
            "sender": "wxid_source",
            "filepath": r"shaoyou11\FileStorage\Cache\2026-08\article.jpg",
            "timestamp": 1900,
            "content": "公众号文章标题",
        },
    }]

    details = delivery_details(tmp_path, now=2000, bridge_messages=active)
    text = format_delivery_details(tmp_path, now=2000, details=details)

    assert len(details["bridge"]) == 1
    assert details["bridge"][0]["type"] == "链接/公众号"
    assert "Bridge 队列：1 条" in text
    assert "Bridge 队列 #1" in text
    assert "微信会话：48182341636@chatroom" in text
    assert "发送者：wxid_source" in text
    assert "发送到：待 EFB 消费后按会话映射" in text
    assert "公众号文章标题" in text
    assert "Bridge 暂存" in text


def test_delivery_summary_reports_bridge_queue_counts(tmp_path):
    result = delivery_summary(tmp_path, {}, bridge={
        "ok": True,
        "queue_size": 3,
        "staged_size": 1,
        "pending_size": 1,
        "inflight_size": 1,
        "dead_letter_size": 2,
    })

    assert result["bridge_available"] is True
    assert result["bridge_active"] == 3
    assert result["bridge_staged"] == 1
    assert result["bridge_pending"] == 1
    assert result["bridge_inflight"] == 1
    assert result["bridge_dead"] == 2


def test_bridge_delivery_target_uses_topic_association():
    ui = OperationsUI.__new__(OperationsUI)
    ui.channel = SimpleNamespace(db=SimpleNamespace(
        get_topic_assocs=lambda source_uid: [(-1001234567890, 22)],
        get_chat_assoc=lambda **kwargs: [],
    ))
    details = {
        "pending": [],
        "failed": [],
        "bridge": [{
            "source_uid": "honus.comwechat 48182341636@chatroom",
            "telegram_target": "待 EFB 消费后按会话映射",
        }],
    }

    ui._resolve_delivery_targets(details)

    assert details["bridge"][0]["telegram_target"] == (
        "Telegram chat_id=-1001234567890，话题=22"
    )


def test_delivery_markup_only_offers_retry_for_readable_files(tmp_path):
    available = tmp_path / "available.jpg"
    available.write_bytes(b"image")
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    (state / "failed-deliveries.json").write_text(json.dumps({
        "available-token": {
            "path": str(available),
            "expires": 3000,
        },
        "missing-token": {
            "path": str(tmp_path / "missing.mp4"),
            "expires": 3000,
        },
    }), encoding="utf-8")

    ui = OperationsUI.__new__(OperationsUI)
    markup = ui.delivery_markup(delivery_details(tmp_path, now=2000))
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "retry:available-token" in callbacks
    assert "retry:missing-token" not in callbacks
    assert "ops:delivery" in callbacks
    assert "ops:diagnostic" in callbacks
    assert "ops:delivery_clear" in callbacks
    assert "ops:close" in callbacks


def test_clear_invalid_delivery_records_only_removes_unreadable_records(tmp_path):
    pending_path = tmp_path / "profiles/comwechat/honus.comwechat"
    pending_path.mkdir(parents=True)
    (pending_path / "pending-files.json").write_text(json.dumps({
        "missing": {"path": str(tmp_path / "missing")},
        "readable": {"path": str(tmp_path / "available.jpg")},
    }), encoding="utf-8")
    (tmp_path / "available.jpg").write_bytes(b"image")

    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    (state / "failed-deliveries.json").write_text(json.dumps({
        "missing-token": {"path": str(tmp_path / "missing.mp4"), "expires": 1000},
        "readable-token": {"path": str(tmp_path / "available.jpg"), "expires": 3000},
    }), encoding="utf-8")

    result = clear_invalid_delivery_records(tmp_path, now=2000)

    assert result["pending"] == 1
    assert result["failed"] == 1
    assert result["backup"].is_file()
    assert json.loads((pending_path / "pending-files.json").read_text()) == {
        "readable": {"path": str(tmp_path / "available.jpg")}
    }
    assert json.loads((state / "failed-deliveries.json").read_text()) == {
        "readable-token": {"path": str(tmp_path / "available.jpg"), "expires": 3000}
    }
    assert (tmp_path / "available.jpg").is_file()


def test_pending_target_uses_topic_association():
    ui = OperationsUI.__new__(OperationsUI)
    ui.channel = SimpleNamespace(db=SimpleNamespace(
        get_topic_assocs=lambda source_uid: [(-1001234567890, 22)],
        get_chat_assoc=lambda **kwargs: [],
    ))
    details = {
        "pending": [{
            "source_uid": "honus.comwechat 48182341636@chatroom",
            "telegram_target": "待处理记录未保存 Telegram 目标",
        }],
        "failed": [],
    }

    ui._resolve_delivery_targets(details)

    assert details["pending"][0]["telegram_target"] == (
        "Telegram chat_id=-1001234567890，话题=22"
    )


def test_diagnostic_path_is_scoped_to_efb_data_root(tmp_path):
    from efb_telegram_master.operations_ui import diagnostic_path

    assert diagnostic_path(tmp_path) == tmp_path / "watchdog/diagnostics/last-login-failure.png"


def test_backup_summary_reports_count_and_latest_without_file_content(tmp_path: Path):
    first = tmp_path / "config-20260718-010000"
    second = tmp_path / "config-20260718-020000"
    first.mkdir()
    second.mkdir()
    os.utime(first, (2000, 2000))
    os.utime(second, (1000, 1000))

    result = backup_summary(tmp_path)

    assert result["count"] == 2
    assert result["latest"] == first.name


def test_human_size_uses_complete_unit_sequence():
    assert _human_size(int(1.5 * 1024**3)) == "1.50 GB"


def test_redact_error_removes_bot_tokens_and_urls():
    text = "request https://host/bot123456:ABC_secret/sendMessage failed"

    assert "ABC_secret" not in redact_error(text)
    assert "https://" not in redact_error(text)


def test_security_scan_returns_key_names_without_values(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("token: very-secret\nadmins: [1]\n", encoding="utf-8")

    findings = scan_sensitive_keys(tmp_path)

    assert findings == [{"file": "config.yaml", "keys": ["token"]}]
    assert "very-secret" not in str(findings)
