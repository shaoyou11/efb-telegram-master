import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from efb_telegram_master.settings_history import SettingsHistory
from efb_telegram_master.delivery_policy import DeliveryPolicy, DeliveryPolicyStore
from efb_telegram_master.operations_ui import OperationsUI


def test_change_and_confirmed_undo_survive_reload(tmp_path):
    path = tmp_path / "history.json"
    history = SettingsHistory(path)
    state = {"enabled": False}
    getter = lambda: state["enabled"]
    setter = lambda value: state.update(enabled=value)
    history.apply("digest", "", getter, setter, True, 1)
    assert state["enabled"] is True
    entry = history.entries()[-1]
    SettingsHistory(path).undo(entry["id"], lambda *_: (getter, setter), 1)
    assert state["enabled"] is False
    assert history.entries()[-1]["undone_at"]
    with pytest.raises(ValueError):
        history.undo(entry["id"], lambda *_: (getter, setter))


def test_undo_refuses_newer_or_external_changes(tmp_path):
    history = SettingsHistory(tmp_path / "history.json")
    state = {"enabled": False}
    getter = lambda: state["enabled"]
    setter = lambda value: state.update(enabled=value)
    history.apply("wechat-read", "", getter, setter, True)
    entry = history.entries()[-1]
    state["enabled"] = False
    with pytest.raises(ValueError, match="其他操作"):
        history.undo(entry["id"], lambda *_: (getter, setter))
    history.apply("wechat-read", "", getter, setter, True)
    with pytest.raises(ValueError, match="记录已变化"):
        history.undo(entry["id"], lambda *_: (getter, setter))


def test_history_allowlist_and_write_failure_preserve_setting(tmp_path, monkeypatch):
    history = SettingsHistory(tmp_path / "history.json")
    state = {"enabled": False}
    getter = lambda: state["enabled"]
    setter = lambda value: state.update(enabled=value)
    with pytest.raises(ValueError):
        history.apply("token", "", getter, setter, "secret")
    assert not history.path.exists()
    monkeypatch.setattr(history, "_save", lambda _: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        history.apply("digest", "", getter, setter, True)
    assert state["enabled"] is False


def test_rule_preview_matches_runtime_and_never_writes(tmp_path):
    store = DeliveryPolicyStore(tmp_path / "policies.json")
    store.set("a", DeliveryPolicy.FILTERED)
    store.set_quiet_hours("22:00", "08:00", True)
    before = store.path.read_bytes()
    for hour in (0, 7, 8, 12, 22, 23):
        for key in ("a", "b"):
            instant = datetime(2026, 9, 3, hour)
            preview = store.explain(key, instant)
            assert preview["effective"] == store.get(key, instant).value
    assert store.path.read_bytes() == before
    assert store.explain("a", datetime(2026, 9, 3, 23))["effective"] == "filtered"
    assert store.explain("b", datetime(2026, 9, 3, 23))["effective"] == "silent"


def test_status_pages_keep_all_sections_without_version_dump_in_settings(tmp_path, monkeypatch):
    ui = OperationsUI(SimpleNamespace(config={"admins": [1]}))
    ui.data_root = tmp_path
    monkeypatch.setattr(ui, "_wechat_login", lambda: "已登录")
    monkeypatch.setattr(ui, "_bot_api", lambda: "正常")
    sent = []
    ui._send = lambda _update, text, *args, **kwargs: sent.append(text)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    for page in ("delivery", "components", "settings"):
        ui.status_page(update, None, page)
    assert all(len(text.encode("utf-16-le")) // 2 < 4096 for text in sent)
    assert "【消息投递】" in sent[0] and "【组件版本】" not in sent[0]
    assert "【运行环境】" in sent[1] and "【版本标识】" in sent[1]
    assert "【巡检与存储】" in sent[2] and "【组件版本】" not in sent[2]
    assert set(ui.health_sections()) == {"runtime", "settings", "delivery", "storage", "components"}


def test_operations_reject_duplicate_running_action():
    ui = OperationsUI(SimpleNamespace(config={"admins": [1]}))
    answers = []
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                             callback_query=SimpleNamespace(answer=lambda *a, **k: answers.append(a)))
    ui._operation_lock.acquire()
    try:
        ui.callback(update, None)
    finally:
        ui._operation_lock.release()
    assert "正在处理上一项操作" in answers[0][0]


def test_contact_list_is_paged_and_details_are_addressable():
    ui = OperationsUI(SimpleNamespace(config={"admins": [1]}))
    records = [{"uid": f"wxid_{index}", "name": f"wxid_{index}"} for index in range(16)]
    ui._contact_snapshot = lambda **_: {"unresolved": records, "aliased": []}
    sent = []
    ui._render = lambda _, text, markup: sent.append((text, markup))
    ui.contact_center(SimpleNamespace(effective_user=SimpleNamespace(id=1)), None)
    text, markup = sent[-1]
    assert "wxid_4" in text and "wxid_5" not in text
    assert "未识别共 16" in text
    actions = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert len([action for action in actions if action.startswith("ops:contact:")]) == 5
    assert "ops:contacts-page:1" in actions
