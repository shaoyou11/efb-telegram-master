import json
import os
from types import SimpleNamespace
from pathlib import Path

from efb_telegram_master.operations_ui import (
    OperationsUI,
    _human_size,
    backup_summary,
    format_timestamp,
    format_uptime,
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


def test_status_markup_exposes_bridge_queue_menu():
    markup = OperationsUI.markup("status", include_bridge=True)

    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "bridgeq:home" in callbacks
