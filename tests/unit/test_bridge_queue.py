import json
from pathlib import Path

import pytest

import efb_telegram_master.bridge_queue as bridge_queue
from efb_telegram_master.bridge_queue import (
    BridgeQueueClient,
    BridgeQueueError,
    BridgeQueueSettings,
)


def test_settings_default_off_and_atomic_round_trip(tmp_path: Path):
    path = tmp_path / "bridge-queue-settings.json"
    settings = BridgeQueueSettings(path)

    assert settings.enabled is False
    settings.enabled = True

    assert json.loads(path.read_text(encoding="utf-8")) == {"management_enabled": True}
    assert BridgeQueueSettings(path).enabled is True


def test_corrupt_settings_fall_back_to_disabled(tmp_path: Path):
    path = tmp_path / "bridge-queue-settings.json"
    path.write_text("not-json", encoding="utf-8")

    assert BridgeQueueSettings(path).enabled is False


def test_settings_reject_non_boolean_value(tmp_path: Path):
    path = tmp_path / "bridge-queue-settings.json"
    path.write_text('{"management_enabled": "false"}', encoding="utf-8")
    settings = BridgeQueueSettings(path)

    assert settings.enabled is False
    settings.enabled = True
    assert settings.enabled is True


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_client_builds_internal_api_payloads(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req.full_url, json.loads(req.data.decode("utf-8")), timeout))
        return FakeResponse({"ok": True, "result": "retried"})

    monkeypatch.setattr(bridge_queue.request, "urlopen", fake_urlopen)
    client = BridgeQueueClient("http://comwechat:19088/")

    assert client.retry_active("message-1") == "retried"
    assert calls == [
        (
            "http://comwechat:19088/v1/messages/retry-active",
            {"message_id": "message-1"},
            5,
        )
    ]


def test_client_builds_paginated_and_batch_requests(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        payload = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append((req.full_url, payload))
        if req.full_url.endswith("/v1/messages/active?limit=5&offset=10"):
            return FakeResponse({"ok": True, "messages": [], "total": 12})
        return FakeResponse({"ok": True, "retried": 2})

    monkeypatch.setattr(bridge_queue.request, "urlopen", fake_urlopen)
    client = BridgeQueueClient("http://comwechat:19088")

    assert client.active_page(5, 10) == ([], 12)
    assert client.retry_all_active() == 2
    assert calls[0][0].endswith("/v1/messages/active?limit=5&offset=10")
    assert calls[1][0].endswith("/v1/messages/retry-all-active")


def test_client_redacts_endpoint_and_token_from_errors(monkeypatch):
    def fake_urlopen(_req, timeout):
        raise RuntimeError(
            "request http://comwechat:19088/bot123456:secret/getMe failed"
        )

    monkeypatch.setattr(bridge_queue.request, "urlopen", fake_urlopen)
    client = BridgeQueueClient("http://comwechat:19088")

    with pytest.raises(BridgeQueueError) as error:
        client.health()

    text = str(error.value)
    assert "comwechat:19088" not in text
    assert "secret" not in text
    assert "http://" not in text
    assert len(text) <= 160
