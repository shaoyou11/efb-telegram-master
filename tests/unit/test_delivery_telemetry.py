import json

from efb_telegram_master.delivery_telemetry import DeliveryTelemetry, recovery_action, sanitize_failure


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
