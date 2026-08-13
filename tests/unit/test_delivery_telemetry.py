import json

from efb_telegram_master.delivery_telemetry import (
    DeliveryTelemetry,
    delivery_stats_summary,
    delivery_trace_summary,
    recovery_action,
    sanitize_failure,
    digest_delta,
    DigestGuard,
)


def test_delivery_telemetry_records_and_clears_pending(tmp_path):
    path = tmp_path / "delivery.json"
    telemetry = DeliveryTelemetry(path)
    telemetry.inbound("message-1", "Image", 100)
    assert json.loads(path.read_text())["pending"]["uid"] == "message-1"

    telemetry.delivered("message-1")
    state = json.loads(path.read_text())
    assert state["pending"] is None
    assert state["last_delivered_at"] > 0


def test_delivery_telemetry_records_recent_latency(tmp_path, monkeypatch):
    path = tmp_path / "delivery.json"
    telemetry = DeliveryTelemetry(path)
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100.0)
    telemetry.inbound("message-1", "Image", 100)

    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 101.25)
    telemetry.delivered("message-1")

    state = json.loads(path.read_text())
    assert state["last_latency_ms"] == 1250


def test_legacy_delivery_state_gets_latency_field(tmp_path):
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps({"pending": None}), encoding="utf-8")

    telemetry = DeliveryTelemetry(path)

    assert telemetry.state["last_latency_ms"] is None


def test_delivery_telemetry_clears_pending_from_previous_process(tmp_path):
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps({
        "pending": {"uid": "stale-message", "type": "Image", "at": 100.0},
        "last_inbound_at": 100.0,
    }), encoding="utf-8")

    telemetry = DeliveryTelemetry(path)

    assert telemetry.state["pending"] is None
    assert json.loads(path.read_text(encoding="utf-8"))["pending"] is None


def test_delivery_telemetry_persists_24_hour_aggregate_stats(tmp_path, monkeypatch):
    path = tmp_path / "delivery.json"
    telemetry = DeliveryTelemetry(path)

    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100000.0)
    telemetry.inbound("message-1", "Image")
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100001.0)
    telemetry.delivered("message-1")

    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100001.5)
    telemetry.inbound("message-2", "Text")
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100002.0)
    telemetry.filtered("message-2")

    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100003.0)
    telemetry.inbound("message-3", "File")
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100005.0)
    telemetry.failed("message-3", "temporary failure")

    result = delivery_stats_summary(tmp_path, now=100005.0)

    assert result == {
        "inbound": 3,
        "delivered": 1,
        "filtered": 1,
        "failed": 1,
        "silent": 0,
        "average_latency_ms": 1167,
    }


def test_digest_delta_only_reports_new_silent_filtered_and_failed_counts():
    current = {"silent": 5, "filtered": 3, "failed": 2}
    previous = {"silent": 4, "filtered": 3, "failed": 0}

    assert digest_delta(current, previous) == {
        "silent": 1,
        "filtered": 0,
        "failed": 2,
    }


def test_digest_guard_establishes_baseline_then_reports_only_new_counts(tmp_path):
    class Bot:
        def __init__(self):
            self.messages = []

        def send_message(self, admin, text):
            self.messages.append((admin, text))

    class Channel:
        config = {"admins": [1]}
        bot_manager = Bot()

    stats = {"silent": 2, "filtered": 1, "failed": 0}
    guard = DigestGuard(Channel(), tmp_path / "digest.json", lambda: stats, interval=60)

    assert guard.check_once(now=100) == "baseline"
    stats.update({"silent": 3, "failed": 1})
    assert guard.check_once(now=401) == "sent"
    assert "静默接收：1 条" in Channel.bot_manager.messages[0][1]
    assert "失败：1 条" in Channel.bot_manager.messages[0][1]


def test_delivery_stats_ignore_buckets_older_than_24_hours(tmp_path, monkeypatch):
    path = tmp_path / "delivery.json"
    telemetry = DeliveryTelemetry(path)
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100000.0)
    telemetry.inbound("old-message", "Text")

    result = delivery_stats_summary(tmp_path, now=100000.0 + 25 * 3600)

    assert result["inbound"] == 0
    assert result["average_latency_ms"] is None


def test_failure_reason_is_redacted():
    result = sanitize_failure("https://host/bot123:secret/send failed at /private/file.jpg")
    assert "secret" not in result
    assert "/private/file.jpg" not in result


def test_failed_delivery_clears_pending_restart_marker(tmp_path):
    path = tmp_path / "delivery.json"
    telemetry = DeliveryTelemetry(path)
    telemetry.inbound("message-1", "File", 100)

    telemetry.failed("message-1", "network error")

    state = json.loads(path.read_text())
    assert state["pending"] is None
    assert state["last_failure"]["uid"] == "message-1"


def test_delivery_trace_keeps_sanitized_recent_stages(tmp_path, monkeypatch):
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 100.0)
    telemetry = DeliveryTelemetry(tmp_path / "delivery.json")
    telemetry.inbound("private-message-id", "Image", 20)
    monkeypatch.setattr("efb_telegram_master.delivery_telemetry.time.time", lambda: 101.0)
    telemetry.delivered("private-message-id")

    traces = delivery_trace_summary(tmp_path, now=101.0)

    assert len(traces) == 1
    assert traces[0]["stage"] == "delivered"
    assert traces[0]["trace_id"] != "private-message-id"
    assert "uid" not in traces[0]


def test_logged_out_wechat_never_restarts_stalled_delivery():
    state = {"pending": {"at": 100.0}}
    assert recovery_action(state, logged_in=False, now=1000.0, last_restart_at=0) == "alert"


def test_logged_in_stall_restarts_once_then_obeys_cooldown():
    state = {"pending": {"uid": "message-1", "at": 100.0}}
    assert recovery_action(state, logged_in=True, now=1000.0, last_restart_at=0) == "restart"
    assert recovery_action(
        state,
        logged_in=True,
        now=5000.0,
        last_restart_at=1000.0,
        last_restart_uid="message-1",
    ) == "alert"


def test_new_stalled_message_can_restart_after_global_cooldown():
    state = {"pending": {"uid": "message-2", "at": 4000.0}}
    assert recovery_action(
        state,
        logged_in=True,
        now=5000.0,
        last_restart_at=1000.0,
        last_restart_uid="message-1",
    ) == "restart"
