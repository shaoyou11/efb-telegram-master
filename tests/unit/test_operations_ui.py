import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

from efb_telegram_master.operations_ui import (
    OperationsUI,
    _human_size,
    backup_summary,
    delivery_summary,
    format_backup_verification,
    format_compact_status,
    format_contact_center,
    format_delivery_stats,
    format_health_action,
    format_latest_match,
    format_manual_restart,
    format_restore_rehearsal_status,
    format_selftest_report,
    format_trace_report,
    format_issues_report,
    format_audit_status,
    format_timestamp,
    format_uptime,
    load_json,
    redact_error,
    request_manual_restart,
    request_restore_rehearsal,
    scan_sensitive_keys,
)


def test_load_json_rejects_invalid_content(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("invalid", encoding="utf-8")
    assert load_json(path) == {}


def test_format_timestamp_handles_missing_value():
    assert format_timestamp(None) == "暂无"


def test_format_latest_match_accepts_legacy_verify_stack_field():
    assert format_latest_match({"ghcr_latest_match": "匹配", "checked_at": 1000}).startswith(
        "匹配（最近校验"
    )


def test_format_trace_report_joins_bridge_and_telegram_without_content():
    text = format_trace_report(
        [{"trace_id": "abcdef123456", "state": "acked", "attempts": 1}],
        [{"trace_id": "abcdef123456", "stage": "delivered", "at": 1000, "type": "Image"}],
    )

    assert "abcdef123456" in text
    assert "Bridge 已确认" in text
    assert "Telegram 已投递" in text
    assert "消息正文" not in text


def test_issues_report_only_lists_actionable_failures():
    text = format_issues_report(
        logged_in=False,
        queue={"pending": 2, "failed": 1},
        bridge={"dead_letter_size": 3},
        audits={"数据库": {"healthy": True}, "容量": {"healthy": False, "reason": "low disk"}},
        mapping_ok=False,
        delivery_stats={"by_type": {"image": {"failed": 2}}},
    )
    assert "微信未登录" in text
    assert "待处理 2" in text
    assert "Bridge 死信 3" in text
    assert "容量" in text
    assert "- 数据库：" not in text
    assert "映射数据库异常" in text
    assert "近24小时失败类型：图片 2" in text


def test_format_uptime_formats_process_runtime():
    assert format_uptime(1000, now=4661) == "1小时 1分钟"


def test_format_audit_status_reports_health_and_missing_check_time():
    assert format_audit_status({"healthy": True}) == "正常（检查时间暂无）"
    assert format_audit_status({"healthy": False}) == "异常（检查时间暂无）"


def test_status_text_summarizes_persistent_reports(tmp_path, monkeypatch):
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    (tmp_path / "backups/config-1").mkdir(parents=True)
    (state / "delivery.json").write_text(json.dumps({
        "last_delivered_at": 1000,
        "last_latency_ms": 1250,
    }), encoding="utf-8")
    (state / "delivery-stats.json").write_text(json.dumps({
        "version": 1,
        "buckets": {
            "0": {
                "inbound": 3,
                "delivered": 1,
                "filtered": 1,
                "failed": 1,
                "latency_ms_total": 3500,
                "latency_count": 3,
            },
        },
    }), encoding="utf-8")
    (state / "health-guard.json").write_text(json.dumps({
        "healthy": True,
        "action": "healthy",
    }), encoding="utf-8")
    (tmp_path / "delivery-reconcile-latest.json").write_text(json.dumps({
        "pending_count": 1,
        "failed_count": 2,
        "healthy": True,
        "checked_at": 1000,
    }), encoding="utf-8")
    (tmp_path / "database-audit-latest.json").write_text(json.dumps({
        "healthy": True,
        "checked_at": 1000,
    }), encoding="utf-8")
    (tmp_path / "capacity-audit-latest.json").write_text(json.dumps({
        "disk": {"free_percent": 75.5},
        "healthy": True,
        "checked_at": 1000,
    }), encoding="utf-8")
    (tmp_path / "upstream-audit-latest.json").write_text(json.dumps({
        "update_count": 3,
        "healthy": True,
        "checked_at": 1000,
    }), encoding="utf-8")
    (state / "image-metadata.json").write_text(json.dumps({
        "build_time": "2026-08-09T13:11:16Z",
        "latest_match": True,
        "checked_at": 4660,
    }), encoding="utf-8")
    (tmp_path / "backup-audit-latest.json").write_text(json.dumps({
        "healthy": True,
        "checked_at": 4660,
        "manifest": {"status": "ok"},
        "sqlite": {"status": "ok"},
        "decrypt": {"status": "not_configured"},
    }), encoding="utf-8")
    (state / "maintenance.json").write_text(json.dumps({
        "enabled": False,
        "phase": "idle",
        "last_result": "success",
        "last_completed_at": 4660,
    }), encoding="utf-8")
    session_path = tmp_path / "profiles/comwechat/honus.comwechat"
    session_path.mkdir(parents=True)
    shanghai = timezone(timedelta(hours=8), "Asia/Shanghai")
    (session_path / "session-events.json").write_text(json.dumps({
        "last_logout_at": datetime(2026, 8, 8, 3, 12, 0, tzinfo=shanghai).timestamp(),
        "last_login_at": datetime(2026, 8, 8, 3, 30, 0, tzinfo=shanghai).timestamp(),
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
            "manual_login_protection": True,
            "startup_grace_seconds": 90,
        }),
        finder_feed_status=lambda: {
            "waiting": 1,
            "requested": 2,
            "processing": 0,
            "failed": 0,
        },
    )
    ui.bridge_queue_ui = SimpleNamespace(client=SimpleNamespace(health=lambda: {
        "staged_size": 0,
        "pending_size": 0,
        "inflight_size": 0,
        "queue_size": 0,
        "dead_letter_size": 1,
    }))
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 4661)

    text = ui.health_text()

    assert "EFB 综合状态" in text
    assert "待处理 1｜失败 2" in text
    assert "NAS 磁盘剩余：75.50%" in text
    assert "待评估上游更新：3 项" in text
    assert "【运行环境】" in text
    assert "运行时间：1小时 1分钟" in text
    assert "群成员姓名隐藏：开启" in text
    assert "最近退出：2026-08-08 03:12:00" in text
    assert "最近登录：2026-08-08 03:30:00" in text
    assert "最近恢复动作：正常" in text
    assert "自动恢复：总开关开启｜全天开启｜凌晨关闭" in text
    assert (
        "恢复配置：\n"
        "  检查间隔：2分钟\n"
        "  点击冷却：2分钟\n"
        "  连续失败：3次后暂停"
    ) in text
    assert "登录保护：扫码保护开启｜启动保护1分30秒" in text
    assert "Bridge 队列：暂存 0｜待投递 0｜处理中 0｜总计 0｜死信 1" in text
    assert "投递审计：正常" in text
    assert "数据库审计：正常" in text
    assert "上游审计：正常" in text
    assert "镜像构建：2026-08-09 21:11:16" in text
    assert "运行版本：" in text
    assert "GHCR latest：匹配" in text
    assert "队列最近延迟：最近完成 1.25 秒" in text
    assert "近24小时投递：\n  微信接收 3\n  Telegram成功 1" in text
    assert "备份校验：正常" in text
    assert "维护模式：关闭" in text
    assert "手动重启：暂无" in text
    assert "视频号任务：等待 1｜请求 2｜处理中 0｜失败 0" in text


def test_compact_status_contains_operational_summary_without_detail_sections():
    text = format_compact_status({
        "uptime": "2小时 3分钟",
        "versions": "EFB 2.1.1.dev1",
        "latest": "匹配",
        "wechat": "已登录",
        "bot_api": "正常",
        "stack": "正常",
        "queue": "待处理 0｜失败 0",
        "bridge": "总计 0｜死信 0",
        "latency": "最近完成 850 毫秒",
        "recovery": "正常",
        "backup": "正常",
        "maintenance": "关闭",
        "restore": "通过",
    })

    assert "EFB 综合状态（精简）" in text
    assert "运行时间：2小时 3分钟" in text
    assert "GHCR latest：匹配" in text
    assert "恢复演练：通过" in text
    assert "【巡检与存储】" not in text


def test_selftest_report_exposes_each_read_only_check_without_secrets():
    text = format_selftest_report([
        {"name": "微信登录", "status": "ok", "detail": "已登录"},
        {"name": "Telegram Bot API", "status": "failed", "detail": "接口暂不可用"},
        {"name": "备份校验", "status": "unknown", "detail": "未检查"},
    ])

    assert "EFB 深度自检" in text
    assert "微信登录：通过（已登录）" in text
    assert "Telegram Bot API：异常（接口暂不可用）" in text
    assert "备份校验：未检查（未检查）" in text
    assert "token" not in text.lower()


def test_contact_center_formats_unresolved_and_local_alias_history():
    text = format_contact_center({
        "unresolved": [{
            "uid": "gh_demo",
            "kind": "联系人",
            "name": "gh_demo",
            "history": ["旧名称"],
        }],
        "aliased": [{
            "uid": "wxid_demo",
            "kind": "群聊",
            "name": "工作群",
            "alias": "本地群",
            "history": ["旧群名"],
        }],
    })

    assert "未识别：1 条" in text
    assert "标识：gh_demo" in text
    assert "历史名称：旧名称" in text
    assert "本地别名：本地群" in text
    assert "旧群名" in text


def test_restore_rehearsal_request_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "restore-rehearsal-request.json"

    first = request_restore_rehearsal(path, now=1000, requested_by=7)
    second = request_restore_rehearsal(path, now=1001, requested_by=7)

    assert first["status"] == "requested"
    assert second["status"] == "requested"
    assert second["request_id"] == first["request_id"]
    assert json.loads(path.read_text(encoding="utf-8"))["requested_by"] == 7


def test_restore_rehearsal_status_is_safe_and_concise():
    assert format_restore_rehearsal_status({"status": "requested"}) == "等待执行"
    assert format_restore_rehearsal_status({"status": "running"}) == "执行中"
    assert format_restore_rehearsal_status({
        "status": "completed",
        "healthy": True,
        "completed_at": 1000,
    }).startswith("通过（最近完成")


def test_status_explains_unknown_time_after_tracking_baseline(tmp_path, monkeypatch):
    session_path = tmp_path / "profiles/comwechat/honus.comwechat"
    session_path.mkdir(parents=True)
    (session_path / "session-events.json").write_text(json.dumps({
        "version": 1,
        "current_state": "online",
        "tracking_started_at": 1000,
    }), encoding="utf-8")

    ui = OperationsUI.__new__(OperationsUI)
    ui.data_root = Path(tmp_path)
    ui.started_at = 1000
    ui.channel = SimpleNamespace()
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 1000)

    text = ui.health_text()

    assert "最近登录：监测开始前已登录（时间未知）" in text
    assert "最近退出：暂无" in text


def test_health_action_is_localized():
    assert format_health_action("healthy") == "正常"
    assert format_health_action("recovered:efb") == "已恢复（EFB）"
    assert format_health_action("restart_failed:full") == "恢复失败（全部服务）"
    assert format_health_action("hold_cooldown") == "等待 ComWechat 内部恢复（冷却中）"
    assert format_health_action("comwechat_recovery_requested") == "已请求 ComWechat 容器内恢复"


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


def test_stale_reconcile_does_not_override_live_delivery_queues(tmp_path, monkeypatch):
    state = tmp_path / "operations/state"
    state.mkdir(parents=True)
    pending_path = tmp_path / "profiles/comwechat/honus.comwechat"
    pending_path.mkdir(parents=True)
    (pending_path / "pending-files.json").write_text("{}", encoding="utf-8")
    (state / "failed-deliveries.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("efb_telegram_master.operations_ui.time.time", lambda: 20000)

    result = delivery_summary(tmp_path, {
        "checked_at": 1000,
        "pending_count": 0,
        "failed_count": 2,
    })

    assert result["pending"] == 0
    assert result["failed"] == 0
    assert result["reconcile_stale"] is True


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


def test_status_markup_keeps_previous_operations_entries():
    markup = OperationsUI.markup("status", include_bridge=True)

    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "ops:delivery" in callbacks
    assert "ops:errors" in callbacks
    assert "ops:diagnostic" in callbacks


def test_status_markup_exposes_compact_detail_and_new_operations():
    markup = OperationsUI.markup("status", include_bridge=True)

    assert [button.text for button in markup.inline_keyboard[0]] == [
        "详细状态", "投递明细", "异常中心",
    ]
    assert [button.text for button in markup.inline_keyboard[1]] == [
        "深度自检", "联系人中心", "失败诊断",
    ]
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert "ops:status-detail" in callbacks
    assert "ops:selftest" in callbacks
    assert "ops:contacts" in callbacks
    assert "ops:restore-rehearsal" in callbacks
    assert "ops:status-close" in callbacks


def test_delivery_stats_format_is_content_free():
    last_success = format_timestamp(1000)
    assert format_delivery_stats({
        "inbound": 2,
        "delivered": 1,
        "filtered": 0,
        "failed": 1,
        "average_latency_ms": 850,
        "p95_latency_ms": 1200,
        "last_success_at": 1000,
        "by_type": {
            "text": {
                "inbound": 2,
                "delivered": 1,
                "filtered": 0,
                "silent": 0,
                "failed": 1,
                "average_latency_ms": 850,
                "p95_latency_ms": 1200,
                "last_success_at": 1000,
            },
        },
    }) == (
        "微信接收 2｜Telegram成功 1｜过滤 0｜静默 0｜失败 1｜"
        f"平均延迟 850 毫秒｜P95延迟 1.20 秒｜最近成功 {last_success}｜"
        f"文本 收2/成1/滤0/默0/败1/均850 毫秒/P95 1.20 秒/最近{last_success}"
    )


def test_backup_verification_format_reports_read_only_checks():
    text = format_backup_verification({
        "healthy": True,
        "checked_at": 1000,
        "manifest": {"status": "ok"},
        "sqlite": {"status": "ok"},
        "decrypt": {"status": "not_configured"},
    })

    assert text.startswith("正常（清单、SQLite、解密未配置）")


def test_manual_restart_request_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "manual-restart.json"

    first = request_manual_restart(path, now=1000, requested_by=7)
    second = request_manual_restart(path, now=1001, requested_by=7)

    assert first["status"] == "requested"
    assert second["status"] == "requested"
    assert second["request_id"] == first["request_id"]
    assert json.loads(path.read_text(encoding="utf-8"))["requested_by"] == 7


def test_manual_restart_status_text_does_not_expose_request_details():
    assert format_manual_restart({"status": "running"}) == "执行中"
    assert format_manual_restart({"status": "completed", "completed_at": 1000}) == (
        f"最近完成 {format_timestamp(1000)}"
    )


def test_status_close_deletes_report_and_source_command():
    deleted = []
    answered = []
    report = SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_id=20,
        delete=lambda: deleted.append((100, 20)),
    )
    query = SimpleNamespace(
        data="ops:status-close",
        message=report,
        answer=lambda: answered.append(True),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7),
    )
    ui = OperationsUI.__new__(OperationsUI)
    ui.channel = SimpleNamespace(
        config={"admins": [7]},
        bot_manager=SimpleNamespace(
            delete_message=lambda chat_id, message_id: deleted.append((chat_id, message_id)),
        ),
    )
    ui._status_source_messages = {(100, 20): (100, 19)}

    ui.callback(update, None)

    assert deleted == [(100, 20), (100, 19)]
    assert answered == [True]


def test_delivery_markup_exposes_pending_and_failed_queues():
    markup = OperationsUI.delivery_markup(1, 2)

    assert [button.text for button in markup.inline_keyboard[0]] == [
        "待处理 1 条", "失败 2 条",
    ]
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [
        "ops:delivery:list:pending:0",
        "ops:delivery:list:failed:0",
        "ops:delivery",
        "ops:close",
    ]


def test_failed_delivery_list_has_view_retry_and_delete_actions():
    markup = OperationsUI.delivery_list_markup(
        "failed",
        [("failure-token", {"filename": "photo.jpg"})],
        page=0,
    )

    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "ops:delivery:view:failed:failure-token" in callbacks
    assert "ops:delivery:retry:failure-token" in callbacks
    assert "ops:delivery:delete:failed:failure-token" in callbacks
