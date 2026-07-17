import json

from efb_telegram_master.delivery_policy import DeliveryPolicy, DeliveryPolicyStore


def test_unknown_chat_defaults_to_normal(tmp_path):
    store = DeliveryPolicyStore(tmp_path / "delivery-policies.json")

    assert store.get("channel chat") is DeliveryPolicy.NORMAL


def test_rule_persists_across_store_instances(tmp_path):
    path = tmp_path / "delivery-policies.json"
    store = DeliveryPolicyStore(path)
    store.set("channel chat", DeliveryPolicy.SILENT, name="测试群", chat_type="group")

    reloaded = DeliveryPolicyStore(path)

    assert reloaded.get("channel chat") is DeliveryPolicy.SILENT
    assert reloaded.list_rules()["channel chat"] == {
        "policy": "silent",
        "name": "测试群",
        "type": "group",
    }


def test_reset_removes_custom_rule(tmp_path):
    store = DeliveryPolicyStore(tmp_path / "delivery-policies.json")
    store.set("channel chat", DeliveryPolicy.FILTERED)

    store.reset("channel chat")

    assert store.get("channel chat") is DeliveryPolicy.NORMAL
    assert store.list_rules() == {}


def test_invalid_file_and_policy_fall_back_to_normal(tmp_path):
    path = tmp_path / "delivery-policies.json"
    path.write_text("not json", encoding="utf-8")
    assert DeliveryPolicyStore(path).get("channel chat") is DeliveryPolicy.NORMAL

    path.write_text(json.dumps({"version": 1, "rules": {
        "channel chat": {"policy": "unknown"}
    }}), encoding="utf-8")
    assert DeliveryPolicyStore(path).get("channel chat") is DeliveryPolicy.NORMAL


def test_each_policy_round_trips(tmp_path):
    path = tmp_path / "delivery-policies.json"
    store = DeliveryPolicyStore(path)

    for policy in DeliveryPolicy:
        store.set("channel chat", policy)
        assert DeliveryPolicyStore(path).get("channel chat") is policy
