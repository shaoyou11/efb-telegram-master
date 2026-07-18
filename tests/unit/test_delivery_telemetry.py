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


def test_failure_reason_is_redacted():
    result = sanitize_failure("https://host/bot123:secret/send failed at /private/file.jpg")
    assert "secret" not in result
    assert "/private/file.jpg" not in result


def test_logged_out_wechat_never_restarts_stalled_delivery():
    state = {"pending": {"at": 100.0}}
    assert recovery_action(state, logged_in=False, now=1000.0, last_restart_at=0) == "alert"


def test_logged_in_stall_restarts_once_then_obeys_cooldown():
    state = {"pending": {"at": 100.0}}
    assert recovery_action(state, logged_in=True, now=1000.0, last_restart_at=0) == "restart"
    assert recovery_action(state, logged_in=True, now=1100.0, last_restart_at=1000.0) == "alert"
