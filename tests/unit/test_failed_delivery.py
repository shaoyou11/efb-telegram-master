from efb_telegram_master.failed_delivery import FailedDeliveryStore


def test_failed_delivery_store_survives_restart(tmp_path):
    path = tmp_path / "failed-deliveries.json"
    store = FailedDeliveryStore(path)
    store.put("token", {"path": "/data/example.doc", "expires": 4102444800})

    restored = FailedDeliveryStore(path)

    assert restored.get("token")["path"] == "/data/example.doc"
    restored.remove("token")
    assert FailedDeliveryStore(path).get("token") is None


def test_failed_delivery_store_prunes_expired_records(tmp_path):
    path = tmp_path / "failed-deliveries.json"
    store = FailedDeliveryStore(path)
    store.put("expired", {"path": "/data/old.doc", "expires": 10})

    store.prune(now=20)

    assert FailedDeliveryStore(path).records == {}
